#!/usr/bin/env python3
"""End-to-end test of the MCP server: launch it over stdio and call the tools like a client would.

Assertions use values that were read off the printed pages (see tools/verify_registers.py's spot checks), so
this test fails if the server starts serving something the manual does not say.

    .venv/bin/python tools/test_mcp_server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILED: list[str] = []
CHECKED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKED
    CHECKED += 1
    if ok:
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} {detail}")


def payload(result) -> dict:
    """Tool result -> dict, whether the SDK returned structured content or a JSON text block."""
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict) and sc:
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("no payload in tool result")


async def main() -> int:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(ROOT, "server", "esp32s3_mcp.py")],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"server: {info.server_info.name} {info.server_info.version}")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            check("8 tools advertised", len(names) == 8, str(names))

            r = payload(await session.call_tool("get_register", {"name": "GDMA_IN_CONF0_CH0_REG"}))
            g = r["registers"][0]
            check("get_register GDMA_IN_CONF0_CH0_REG -> 0x0000 R/W p369",
                  g["offset"] == "0x0000" and g["access"] == "R/W" and g["citation"]["page"] == 369,
                  json.dumps(g)[:160])
            check("citation names the document and version",
                  g["citation"]["document"].startswith("ESP32-S3 Technical Reference Manual")
                  and g["citation"]["version"] == "1.8", "")

            r = payload(await session.call_tool("get_register", {"name": "IO_MUX_GPIO0"}))
            check("substring lookup without _REG works",
                  any(x["name"] == "IO_MUX_GPIO0_REG" and x["offset"] == "0x0004" for x in r["registers"]),
                  json.dumps(r)[:160])

            r = payload(await session.call_tool("get_register", {"name": "RNG_DATA_REG",
                                                                "include_base_guess": True}))
            g = r["registers"][0]
            check("absolute-address chapter is flagged, not treated as an offset",
                  g["offset"] == "0x6003_507C" and g["address_is_absolute"] is True
                  and "address_note" in g, json.dumps(g)[:200])
            check("absolute address gets no base-address guess", g.get("base_guess") is None, "")

            r = payload(await session.call_tool("get_register", {"name": "GDMA_IN_CONF0_CH0_REG",
                                                                "include_base_guess": True}))
            bg = r["registers"][0].get("base_guess")
            check("base guess is labelled heuristic and cites Table 4.3-3 (p408-409)",
                  bg is not None and "heuristic" in bg["confidence"]
                  and bg["citation"]["page"] in (408, 409),
                  json.dumps(bg)[:200] if bg else "none")

            r = payload(await session.call_tool("get_register", {"name": "NO_SUCH_REGISTER"}))
            check("unknown register reports a hint instead of inventing one",
                  r["found"] is False and "hint" in r, json.dumps(r)[:120])

            r = payload(await session.call_tool("list_registers", {"prefix": "IO_MUX_GPIO",
                                                                   "limit": 200}))
            check("list_registers by prefix finds the IO MUX bank (38+ rows)",
                  r["total_matching"] >= 38, json.dumps(r["total_matching"]))

            r = payload(await session.call_tool("get_instruction", {"name": "EE.VMULAS.S16.QACC"}))
            i = r["instructions"][0]
            check("get_instruction returns encoding + syntax + page",
                  i["instruction_word"] and i["assembler_syntax"] and 76 <= i["citation"]["page"] <= 303,
                  i["name"])

            r = payload(await session.call_tool("instruction_pipeline", {"name": "EE.ANDQ"}))
            check("instruction_pipeline EE.ANDQ -> qx,qy use / qa def on p66",
                  [o["reg"] for o in r["operands_use"]] == ["qx", "qy"]
                  and [o["reg"] for o in r["operands_def"]] == ["qa"]
                  and r["citation"]["page"] == 66, json.dumps(r["operands_use"]))

            r = payload(await session.call_tool("instruction_pipeline", {"name": "LD.QR"}))
            check("LF.QR pipeline is reported as absent from Table 1.7-2 (not guessed)",
                  r["found"] is False and "no primary source" in r["hint"], json.dumps(r)[:140])

            r = payload(await session.call_tool("list_peripherals", {"target": "UART"}))
            check("list_peripherals finds UART Controller 0 at 0x6000_0000",
                  any(p["target"] == "UART Controller 0" and p["low_address"] == "0x6000_0000"
                      for p in r["peripherals"]), json.dumps(r["peripherals"])[:160])

            r = payload(await session.call_tool("search_manual", {"query": "5-stage pipeline"}))
            check("search_manual finds the pipeline description with a page",
                  r["pages_with_match"] >= 1 and r["hits"][0]["citation"]["page"] == 65,
                  json.dumps(r)[:160])

            r = payload(await session.call_tool("analyze_sequence",
                                                 {"instructions": ["EE.LD.ACCX.IP", "EE.SRS.ACCX"]}))
            check("analyze_sequence: ACCX written at M, read at E -> 1 stall (p65 rule, p66 table)",
                  r["stall_cycles_total"] == 1 and r["issue_cycles_estimate"] == 3
                  and r["pairs"][0]["conflicts"][0]["register"] == "ACCX"
                  and r["pairs"][0]["conflicts"][0]["producer_def_stage"] == 2
                  and r["pairs"][0]["conflicts"][0]["consumer_use_stage"] == 1,
                  json.dumps(r["pairs"][0])[:200])
            check("analyze_sequence carries the rule and flags the W=2/W=3 inconsistency",
                  r["rule"]["citation"]["page"] == 65 and "W as stage 2" in r["rule"]["manual_inconsistency"],
                  json.dumps(r["rule"])[:120])

            r = payload(await session.call_tool("analyze_sequence",
                                                 {"instructions": ["EE.ANDQ", "EE.ANDQ", "EE.XORQ"]}))
            check("analyze_sequence: E->E pairs with no overlap cost nothing",
                  r["stall_cycles_total"] == 0 and r["issue_cycles_estimate"] == 3, "")

            r = payload(await session.call_tool("analyze_sequence",
                                                 {"instructions": ["EE.VRELU.S16", "EE.MOV.S16.QACC"]}))
            check("analyze_sequence: qs written at M then read at E -> 1 stall",
                  r["stall_cycles_total"] == 1, json.dumps(r["pairs"][0])[:160])

            r = payload(await session.call_tool("analyze_sequence",
                                                 {"instructions": ["LD.QR", "ADD", "EE.ANDQ"]}))
            statuses = [x["status"] for x in r["records"]]
            check("analyze_sequence: untabulated (LD.QR) and non-PIE (ADD) instructions are excluded, "
                  "not guessed",
                  statuses[0] == "no_primary_source" and statuses[1] == "not_a_pie_instruction"
                  and r["stall_cycles_total"] == 0 and len(r["unmodelled"]["items"]) == 2,
                  json.dumps(statuses))
            check("analyze_sequence: resource/control hazards are reported as unmodelled with citations",
                  r["unmodelled"]["hardware_resource_citation"]["page"] == 74
                  and r["unmodelled"]["control_hazard_citation"]["page"] == 74, "")

            r = payload(await session.call_tool("get_page", {"page": 66}))
            check("get_page(66) returns Table 1.7-2's page",
                  "Extended Instruction Pipeline Stages" in r["text"], r["text"][:80])

    print(f"\n{'FAILED' if FAILED else 'PASSED'}: {len(FAILED)} failure(s) of {CHECKED} checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
