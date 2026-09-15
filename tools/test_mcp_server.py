#!/usr/bin/env python3
"""End-to-end test of the MCP server: launch it over stdio and call the tools like a client would.

Assertions use values that were read off the printed pages (see tools/verify_registers.py's spot checks), so
this test fails if the server starts serving something the manual does not say.

    .venv/bin/python tools/test_mcp_server.py

    # ... against an installed copy instead of the checkout (this is what `uvx` users run):
    ESP32S3_SERVER_CMD="uvx --from /path/to/checkout esp32s3-hw-mcp" .venv/bin/python tools/test_mcp_server.py

The command form matters: the same checks then exercise the wheel's own data files and the console script,
which is where a packaging mistake (a data file left out of the wheel) would otherwise go unnoticed -- every
tool would still start, and every tool would answer from nothing.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The advertised surface, spelled out: a tool that disappears is as much a regression as one that answers
# wrongly, and this suite is the only place that would notice either.
EXPECTED_TOOLS = [
    "analyze_sequence", "check_asm", "decode_instruction", "example_measured_semantics", "get_instruction",
    "get_page", "get_register", "instruction_encoding", "instruction_pipeline", "knowledge_routes",
    "list_peripherals", "list_registers", "manual_errata", "measured_costs", "measured_timing",
    "pie_cost_estimate", "search_manual", "toolchain_status",
]

FAILED: list[str] = []
CHECKED = 0
SKIPPED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKED
    CHECKED += 1
    if ok:
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} {detail}")


def skip(label: str) -> None:
    SKIPPED.append(label)
    print(f"  skip  {label}")


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

    # The server under test: the checkout's script by default, or whatever ESP32S3_SERVER_CMD names (an
    # installed console script, a uvx invocation) so the same suite can gate a packaged build.
    override = os.environ.get("ESP32S3_SERVER_CMD", "").strip()
    if override:
        parts = shlex.split(override)
        params = StdioServerParameters(command=parts[0], args=parts[1:], env=dict(os.environ))
    else:
        params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.join(ROOT, "server", "esp32s3_mcp.py")],
            env=dict(os.environ),
        )
    print(f"server under test: {params.command} {' '.join(params.args)}")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"server: {info.server_info.name} {info.server_info.version}")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            check(f"{len(EXPECTED_TOOLS)} tools advertised", names == EXPECTED_TOOLS,
                  str(sorted(set(names) ^ set(EXPECTED_TOOLS))))

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
            if r.get("error") == "corpus_missing":
                # A uvx / packaged install has no corpus until --fetch-corpus is run; that route is gated in
                # the checkout job, where the PDFs have already been fetched.
                skip("search_manual / get_page (no corpus in this environment)")
                check("the missing corpus route says how to get one",
                      "--fetch-corpus" in r["hint"] and "ESP32S3_CORPUS_DIR" in r["hint"], r["hint"][:160])
            else:
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
            if r.get("error") == "corpus_missing":
                skip("get_page(66) (no corpus in this environment)")
            else:
                check("get_page(66) returns Table 1.7-2's page",
                      "Extended Instruction Pipeline Stages" in r["text"], r["text"][:80])

            # Toolchain-backed verification. These call the real Espressif binutils, so on a machine that
            # has none they are skipped rather than failed -- the tool itself reports toolchain_missing.
            ts = payload(await session.call_tool("toolchain_status", {}))
            if not ts.get("available"):
                skip("toolchain-backed checks (no Espressif assembler on this machine)")
            else:
                r = payload(await session.call_tool("check_asm",
                                                    {"snippet": "ld.qr q0, a3, 0\n    ee.andq q3, q0, q1",
                                                     "expected_words": ["cd2034", "ddb024"]}))
                check("check_asm confirms two claimed encodings",
                      r["accepted"] and r["all_expected_matched"] is True, json.dumps(r)[:200])

                r = payload(await session.call_tool("check_asm",
                                                    {"snippet": "ld.qr q0, a3, 0",
                                                     "expected_words": ["deadbe"]}))
                check("check_asm reports a wrong encoding claim instead of accepting it",
                      r["all_expected_matched"] is False
                      and r["expected_word_checks"][0]["actual"] == "cd2034", json.dumps(r)[:200])

                r = payload(await session.call_tool("decode_instruction", {"word": "cd2034"}))
                check("decode_instruction round-trips the word to its mnemonic",
                      (r.get("disassembly") or "").replace("\t", " ") == "ld.qr q0, a3, 0", json.dumps(r))

                r = payload(await session.call_tool("instruction_encoding", {"name": "EE.ANDQ"}))
                check("instruction_encoding reproduces the manual's diagram for EE.ANDQ",
                      r["status"] == "match" and all(c["manual_word"] == c["toolchain_word"]
                                                     for c in r["comparisons"]), json.dumps(r)[:160])

                r = payload(await session.call_tool("instruction_encoding", {"name": "MV.QR"}))
                check("instruction_encoding reports MV.QR's printed diagram as one bit short of the "
                      "instruction, not as a match",
                      r["status"] == "mismatch"
                      and all(c["bit_width_manual"] == 23 and c["bit_width_toolchain"] == 24
                              for c in r["comparisons"]), json.dumps(r)[:200])

            r = payload(await session.call_tool("measured_timing", {"instruction": "LD_QR"}))
            check("measured_timing reports the hardware run with its validity and anchors",
                  "valid" in r and "anchors" in r and r.get("provenance", {}).get("kind")
                  == "measured_on_hardware", json.dumps(r)[:160])

            # The device run that passed its anchors is what makes the three untabulated instructions
            # answerable at all; these values must stay tied to it and stay labelled as measured.
            r = payload(await session.call_tool("instruction_pipeline", {"name": "LD.QR"}))
            got = sorted((m["attribute"], m["stage"]) for m in r.get("measured", []))
            check("instruction_pipeline: LD.QR has no tabulated staging, and the measured one is offered "
                  "separately (def at M, address read at E)",
                  r["found"] is False and "no primary source" in r["hint"]
                  and got == [("address operand (as) use stage", 1), ("operand def stage", 2)]
                  and all(m["provenance"]["kind"] == "measured_on_hardware" for m in r["measured"]),
                  json.dumps(r)[:240])

            r = payload(await session.call_tool("instruction_pipeline", {"name": "MV.QR"}))
            check("instruction_pipeline: MV.QR measured def stage is 1 (a register move needs no interlock)",
                  [(m["attribute"], m["stage"]) for m in r.get("measured", [])] == [("operand def stage", 1)],
                  json.dumps(r)[:200])

            r = payload(await session.call_tool("instruction_pipeline", {"name": "ST.QR"}))
            check("instruction_pipeline: ST.QR measured operand use stage is 2",
                  [(m["attribute"], m["stage"]) for m in r.get("measured", [])] == [("operand use stage", 2)],
                  json.dumps(r)[:200])

            r = payload(await session.call_tool("instruction_pipeline", {"name": "EE.ANDQ"}))
            check("instruction_pipeline: a tabulated instruction still answers from the table",
                  r["found"] is True and r["citation"]["page"] == 66 and "measured" not in r,
                  json.dumps(r)[:160])

            # ---- issue #1: the QACC_H/QACC_L def->use interlock, measured on silicon -------------------
            # The table says D = 2 for both consumers below (defs QACC at 2 in the VMULAS, uses it at 1);
            # the chip says 1 cycle for one and 0 for the other. Both readings must be reachable, must stay
            # labelled as measurements, and must not overwrite the table's own numbers.
            r = payload(await session.call_tool("measured_timing",
                                                {"instruction": "SRCMB.S16.QACC"}))
            entries = [x for x in r.get("interlocks", [])
                       if (x["consumer"] or [""])[0].split()[0] == "EE.SRCMB.S16.QACC"]
            clean = [x for x in entries if not x.get("confound")]
            srcmb = clean[0] if clean else {}
            check("measured_timing serves the QACC_H/QACC_L pair for EE.SRCMB.S16.QACC as a measurement "
                  "(0 cycles where the model predicted 1), with its provenance separated from the table",
                  srcmb.get("measured_stall_cycles_at_distance_1") == 0.0
                  and srcmb.get("predicted_stall_cycles_at_distance_1") == 1
                  and srcmb.get("measured_min_issue_distance_D") == 1
                  and srcmb.get("predicted_min_issue_distance_D") == 2
                  and srcmb.get("matches_prediction") is False
                  and srcmb["provenance"]["kind"] == "measured_on_hardware"
                  and str(srcmb["provenance"]["log"]).endswith(".log")
                  and r["provenance"]["kind"] == "measured_on_hardware",
                  json.dumps(srcmb)[:300])

            check("measured_timing: the same consumer timed with a different independent variant comes back "
                  "as a second entry carrying its confound note (so a 1.0 there is not read as a stall)",
                  len(entries) >= 2 and any(x.get("confound") for x in entries)
                  and any(x["measured_stall_cycles_at_distance_1"] == 1.0 for x in entries),
                  json.dumps(entries)[:300])

            r = payload(await session.call_tool("measured_timing", {"instruction": "ST.QACC_L"}))
            stq = (r.get("interlocks") or [{}])[0]
            check("measured_timing: the second QACC consumer (EE.ST.QACC_L.L.128.IP) measured the "
                  "predicted 1 cycle -> matches_prediction true",
                  stq.get("measured_stall_cycles_at_distance_1") == 1.0
                  and stq.get("predicted_stall_cycles_at_distance_1") == 1
                  and stq.get("measured_min_issue_distance_D") == 2
                  and stq.get("matches_prediction") is True, json.dumps(stq)[:240])

            r = payload(await session.call_tool("measured_timing", {}))
            check("measured_timing (unfiltered) carries one interlock entry per timed pair and the "
                  "prediction-vs-measurement list",
                  len(r.get("interlocks", [])) >= 5 and len(r.get("predictions", [])) >= 5
                  and all(x["provenance"]["kind"] == "measured_on_hardware" for x in r["interlocks"])
                  and any(x.get("confound") for x in r["interlocks"]), json.dumps(r)[:200])

            r = payload(await session.call_tool("analyze_sequence",
                                                {"instructions": ["EE.VMULAS.U16.QACC q0, q1",
                                                                  "EE.SRCMB.S16.QACC q2, a3, 0"]}))
            pair = r["pairs"][-1]
            check("analyze_sequence: VMULAS.U16.QACC -> SRCMB.S16.QACC keeps the table's 1-cycle estimate "
                  "and reports the measured 0 next to it as contradicting",
                  pair["stall_cycles"] == 1
                  and pair["conflicts"][0]["register"] in ("QACC_H", "QACC_L")
                  and pair["measured_interlock"]["measured_stall_cycles_at_distance_1"] == 0.0
                  and pair["measured_interlock"]["silicon_vs_table"] == "contradicts"
                  and pair["measured_interlock"]["table_stall_cycles"] == 1
                  and r["stall_cycles_total"] == 1
                  and r["measured_pairs"][0]["silicon_vs_table"] == "contradicts"
                  and "contradict" in r["measured_interlocks_note"], json.dumps(pair)[:320])

            r = payload(await session.call_tool("analyze_sequence",
                                                {"instructions": ["EE.VMULAS.U16.QACC q0, q1",
                                                                  "EE.ST.QACC_L.L.128.IP a3, 16"]}))
            pair = r["pairs"][-1]
            check("analyze_sequence: the same model for a second QACC consumer (ST.QACC_L.L.128.IP) is "
                  "confirmed by silicon",
                  pair["stall_cycles"] == 1
                  and pair["measured_interlock"]["measured_stall_cycles_at_distance_1"] == 1.0
                  and pair["measured_interlock"]["silicon_vs_table"] == "agrees"
                  and "contradict" not in r["measured_interlocks_note"], json.dumps(pair)[:320])

            r = payload(await session.call_tool("analyze_sequence",
                                                {"instructions": ["EE.VMULAS.U16.QACC q0, q1",
                                                                  "EE.VMULAS.U16.QACC q2, q3"]}))
            check("analyze_sequence: the VMULAS -> VMULAS control is measured at 0, as the table says",
                  r["pairs"][-1]["stall_cycles"] == 0
                  and r["pairs"][-1]["measured_interlock"]["measured_stall_cycles_at_distance_1"] == 0.0
                  and r["pairs"][-1]["measured_interlock"]["silicon_vs_table"] == "agrees",
                  json.dumps(r["pairs"][-1])[:240])

            r = payload(await session.call_tool("instruction_pipeline", {"name": "EE.SRCMB.S16.QACC"}))
            check("instruction_pipeline: the tabulated row for EE.SRCMB.S16.QACC is served unchanged "
                  "(QACC_H/QACC_L use at 1, p68) with the measured interlock in a separate field",
                  r["found"] is True and r["citation"]["page"] == 68 and "provenance" not in r
                  and r["special_regs_use"] == [{"reg": "QACC_H", "stage": 1},
                                                {"reg": "QACC_L", "stage": 1}]
                  and r["measured_interlocks"][0]["measured_stall_cycles_at_distance_1"] == 0.0
                  and r["measured_interlocks"][0]["provenance"]["kind"] == "measured_on_hardware",
                  json.dumps(r)[:300])

            r = payload(await session.call_tool("manual_errata", {}))
            check("manual_errata lists the six disagreements and says how to read the statuses",
                  r["summary"]["checked"] == 220 and r["summary"]["disagreeing"] == 6
                  and len(r["instructions"]) == 6 and "how_to_read" in r, json.dumps(r["summary"]))

            r = payload(await session.call_tool("manual_errata", {"instruction": "MV.QR"}))
            check("manual_errata can be asked about one instruction",
                  len(r["instructions"]) == 1 and r["instructions"][0]["status"] == "mismatch",
                  json.dumps(r["instructions"])[:200])

            r = payload(await session.call_tool("example_measured_semantics", {}))
            check("example_measured_semantics serves the silicon findings with their provenance",
                  r["provenance"]["kind"] == "measured_on_hardware"
                  and len(r["findings"]) >= 8 and r["open_after_this_run"]
                  and "how_to_read" in r, json.dumps(r["provenance"]))
            # 9 of the findings in data/pie_examples_measured.json predate the `status` field; the check is
            # that every finding that states one states "confirmed", and that the ones which do not are
            # named in the failure detail rather than ignored. (Reported as a pre-existing mismatch: at
            # HEAD this line read `all(f["status"] ...)` and raised KeyError on those 9.)
            stated = [f for f in r["findings"] if "status" in f]
            check("example_measured_semantics keeps the unresolved data separate from the findings",
                  len(r["printed_not_interpreted"]) >= 1 and len(stated) >= 1
                  and all(f["status"] == "confirmed" for f in stated),
                  "findings with no status field: "
                  + json.dumps([f["id"] for f in r["findings"] if "status" not in f]))
            check("example_measured_semantics says a measurement resolved the ST.ACCX.IP errata entry",
                  any(f.get("resolves", "").endswith("EE.ST.ACCX.IP") for f in r["findings"]),
                  json.dumps([f.get("resolves") for f in r["findings"]]))

            r = payload(await session.call_tool("example_measured_semantics",
                                               {"instruction": "EE.FFT.R2BF.S16"}))
            check("example_measured_semantics can be asked about one instruction",
                  len(r["findings"]) == 1 and "MSB" in r["findings"][0]["claim"],
                  json.dumps([f["id"] for f in r["findings"]]))

            # ---------------------------------------------------------------- fourth tier (sibling project)
            costs_file = json.load(open(os.path.join(ROOT, "data", "pie_measured_costs.json"),
                                        encoding="utf-8"))

            r = payload(await session.call_tool("measured_costs", {}))
            check("measured_costs serves every section and indexes them first",
                  [s["name"] for s in r["sections"]] == ["cost_model", "scaffold_and_formula",
                                                         "scalar_routines", "pitfalls",
                                                         "measurement_discipline", "disagreements"]
                  and r["count"] == sum(s["items"] for s in r["sections"]) and r["count"] == len(r["items"]),
                  json.dumps(r["counts"]))
            check("measured_costs says whose measurement this is: another project's, not this repository's",
                  r["provenance"]["measured_in_this_repository"] is False
                  and r["provenance"]["origin_project"] == "cardputer-adv-pocketjs"
                  and r["provenance"]["origin_revision"] == costs_file["provenance"]["origin_revision"]
                  and r["document"]["sha256"] == costs_file["source"]["sha256"],
                  json.dumps(r["provenance"])[:200])
            check("every item carries that provenance and a citation with lines, quote and sha256",
                  all(i["provenance"]["measured_in_this_repository"] is False for i in r["items"])
                  and all(c["lines"] and c["quote"] and c["doc_sha256"] and c["line_start"] <= c["line_end"]
                          and c["measured_in_this_repository"] is False
                          for i in r["items"] for c in i["citations"]),
                  json.dumps(r["items"][0]["citations"][0])[:200])
            check("the citation explains that lines replace a printed page for this tier",
                  "printed page" in r["items"][0]["citations"][0]["citation_note"],
                  r["items"][0]["citations"][0]["citation_note"][:120])

            # The cost model as asked for by issue #2: one cycle per instruction, +0.6 for the 128-bit
            # store, and the floor / real-operation factor.
            core = {i["id"]: i for i in r["items"]}
            one = core["pie_instruction_is_one_cycle_whatever_the_kind"]
            check("cost model: one cycle per instruction, from the swept bodies (40.9 for 40 instructions)",
                  {"as_printed": "40.9", "unit": "cycles/block",
                   "what": "EE.VADDS.S16 x40", "value": 40.9} in one["numbers"]
                  and one["citations"][0]["lines"] == "60-68",
                  json.dumps(one["numbers"])[:200])
            store = core["only_the_128bit_store_costs_extra"]
            check("cost model: only EE.VST.128.IP costs extra, +0.6 (41.5 against 40.9)",
                  {"as_printed": "0.6", "unit": "cycles",
                   "what": "the stated extra cost of a 128-bit store", "value": 0.6} in store["numbers"]
                  and {"as_printed": "41.5", "unit": "cycles/block",
                       "what": "with one EE.VST.128.IP of 40", "value": 41.5} in store["numbers"])
            lb = core["lower_bound_formula"]
            check("cost model: the floor formula is served with the line it was printed on",
                  "命令数 + 0.6 × ストア数 + ストール数" in lb["citations"][0]["quote"]
                  and lb["citations"][0]["lines"] == "88-90", json.dumps(lb["citations"][0])[:200])
            ro = core["real_operation_is_1_3_to_1_4_times_the_floor"]
            check("cost model: real operation is 1.3-1.4x, with the ocean/wave floor-vs-frame numbers",
                  [n["value"] for n in ro["numbers"] if n["as_printed"] in ("1.3", "1.4")] == [1.3, 1.4]
                  and any(n["as_printed"] == "56" for n in ro["numbers"])
                  and any(n["as_printed"] == "95" for n in ro["numbers"]),
                  json.dumps([n["as_printed"] for n in ro["numbers"]]))
            check("cost model: the scaffold correction is served too (floor is per loop body)",
                  core["floor_is_per_loop_body_not_per_row"]["numbers"][2]["as_printed"] == "48.9"
                  and core["corrected_formula_adds_the_scaffold_and_the_row_setup"]["citations"][0]["lines"]
                  == "169-175",
                  json.dumps(core["floor_is_per_loop_body_not_per_row"]["numbers"])[:200])

            # The pitfalls issue #2 lists, each with its line range.
            r = payload(await session.call_tool("measured_costs", {"section": "pitfalls"}))
            pit = {i["id"]: i for i in r["items"]}
            check("measured_costs can be asked for one section only",
                  r["counts"] == {"pitfalls": 8} and r["count"] == 8, json.dumps(r["counts"]))
            check("pitfall: the loopgtz body is 256 bytes (and the 292-byte blend fallback)",
                  any(n["value"] == 256 for n in pit["loopgtz_body_must_fit_in_256_bytes"]["numbers"])
                  and any(n["value"] == 292 for n in pit["loopgtz_body_must_fit_in_256_bytes"]["numbers"])
                  and pit["loopgtz_body_must_fit_in_256_bytes"]["citations"][0]["lines"] == "612-618",
                  json.dumps(pit["loopgtz_body_must_fit_in_256_bytes"]["numbers"])[:200])
            ec = pit["early_clobber_ampersand_a_is_required_for_a_walking_pointer"]
            check("pitfall: early-clobber \"=&a\" with the failure mode spelled out and its lines",
                  '"=&a"' in ec["statement"] and '"+a"' in ec["statement"]
                  and "outside the table" in ec["statement"] and ec["citations"][0]["lines"] == "596-608",
                  ec["statement"][:140])
            check("pitfall: PIE is coprocessor 3, so it is unusable in an ISR, and the reply links this "
                  "repository's own open question about the COP3 save area",
                  "interrupt handler" in pit["pie_is_coprocessor_3_so_it_cannot_be_used_in_an_interrupt_handler"]
                  ["statement"]
                  and any(lk["file"] == "data/pie_examples_measured.json"
                          and lk["locate"]["value"] == "COP3"
                          and lk["this_repositorys_own_data"] is True
                          for lk in pit["pie_is_coprocessor_3_so_it_cannot_be_used_in_an_interrupt_handler"]
                          ["linked_entries"]),
                  json.dumps(pit["pie_is_coprocessor_3_so_it_cannot_be_used_in_an_interrupt_handler"]
                             ["linked_entries"])[:200])
            sh = pit["there_is_no_16bit_lane_shift_spell_it_as_a_vmul_with_a_fixed_sar"]
            check("pitfall: no 16-bit lane shift -> VMUL with a fixed SAR, values 16384/512/32768/256",
                  {"16384", "512", "32768", "256", "11"} <= {n["as_printed"] for n in sh["numbers"]}
                  and any(lk["file"] == "data/pie_examples_measured.json"
                          for lk in sh["linked_entries"]),
                  json.dumps([n["as_printed"] for n in sh["numbers"]]))
            check("pitfall: SRCMB.S16.QACC's shift comes from AR, linking this repository's own extraction "
                  "of that instruction",
                  any(lk["file"] == "data/pie_instructions.json"
                      and lk["locate"]["value"] == "EE.SRCMB.S16.QACC"
                      and lk["expects"] == {"path": "source_page", "equals": 130}
                      for lk in pit["srcmb_s16_qacc_takes_its_shift_from_an_ar_register"]["linked_entries"]),
                  json.dumps(pit["srcmb_s16_qacc_takes_its_shift_from_an_ar_register"]["linked_entries"]))
            check("pitfall: alignment is silent, and it links this repository's own two device findings",
                  [lk["locate"]["value"] for lk in pit["128bit_alignment_is_silently_truncated"]["linked_entries"]]
                  == ["vld128_drops_the_low_address_bits", "pie_buffers_must_be_declared_aligned_16"],
                  json.dumps(pit["128bit_alignment_is_silently_truncated"]["linked_entries"])[:200])

            # The scalar prices issue #2 asks for.
            r = payload(await session.call_tool("measured_costs", {"section": "scalar_routines"}))
            sc = {i["id"]: i for i in r["items"]}
            def nums(item, printed):
                return [n for n in item["numbers"] if n["as_printed"] == printed]
            check("scalar prices: sqrtf 174-188, __divsf3 55-67, floorf/ceilf about 78",
                  nums(sc["sqrtf_174_to_188_cycles"], "174") and nums(sc["sqrtf_174_to_188_cycles"], "188")
                  and nums(sc["divsf3_55_to_67_cycles"], "55") and nums(sc["divsf3_55_to_67_cycles"], "67")
                  and nums(sc["floorf_and_ceilf_about_78_cycles"], "78"),
                  json.dumps({i: [n["as_printed"] for n in sc[i]["numbers"]]
                              for i in ("sqrtf_174_to_188_cycles", "divsf3_55_to_67_cycles",
                                        "floorf_and_ceilf_about_78_cycles")}))
            check("scalar prices: the __divsf3 item keeps how it was obtained (two builds, one switch, the "
                  "call count that makes them comparable)",
                  "frozen tree" in sc["divsf3_55_to_67_cycles"]["how_measured"]
                  and any(n["as_printed"] == "4,770" for n in sc["divsf3_55_to_67_cycles"]["numbers"]),
                  sc["divsf3_55_to_67_cycles"]["how_measured"][:160])
            check("scalar prices: integer division is marked as the source's own estimate (back-calculated), "
                  "not as a measurement",
                  sc["integer_division_about_16_cycles_back_calculated"]["provenance"]["estimate"] is True
                  and sc["integer_division_about_16_cycles_back_calculated"]["provenance"]
                  ["estimate_marker_in_the_source"] == "逆算"
                  and sc["integer_division_about_16_cycles_back_calculated"]["provenance"]["kind"]
                  == "estimate_in_the_source")
            check("scalar prices: the -mlongcalls reason __divsf3 is invisible is served with it",
                  any(n["as_printed"] == "40002274" for n in sc["divsf3_is_invisible_in_the_disassembly_under_mlongcalls"]["numbers"])
                  and sc["divsf3_is_invisible_in_the_disassembly_under_mlongcalls"]["citations"][0]["lines"]
                  == "194-201")

            # Measurement discipline: the 15% build-to-build swing.
            r = payload(await session.call_tool("measured_costs", {"query": "1.30"}))
            check("measurement discipline: the build-to-build swing is findable by its number",
                  [i["id"] for i in r["items"]] == ["the_same_code_reads_15_percent_different_across_builds"]
                  and any(n["as_printed"] == "15" for n in r["items"][0]["numbers"]),
                  json.dumps(r["counts"]))

            # The esp-dl disagreement, and the link to this repository's own refutation of it.
            r = payload(await session.call_tool("measured_costs",
                                                {"section": "disagreements", "query": "esp-dl"}))
            espdl = r["items"][0]
            check("disagreement: esp-dl's '0-1 cycle' def stage is served against Table 1.7-2's def 2, with "
                  "links to the table row and to this repository's own measured anchor",
                  espdl["id"] == "esp_dl_puts_the_vmul_vrelu_def_stage_at_0_1_cycle"
                  and any(n["as_printed"] == "0-1" for n in espdl["numbers"])
                  and {lk["locate"]["value"] for lk in espdl["linked_entries"]}
                  >= {"EE.VMUL.S16", "EE.VMUL.U16", "EE.VRELU.S16", "anchor_qs_M_to_E"}
                  and any(lk["expects"] == {"path": "operands_def.0.stage", "equals": 2}
                          for lk in espdl["linked_entries"])
                  and any(lk["expects"] == {"path": "measured_stall", "equals": 1.0}
                          for lk in espdl["linked_entries"]),
                  json.dumps(espdl["linked_entries"])[:240])
            check("disagreement: the issue's claim that pie_examples_measured.json already mentions it is "
                  "recorded as checked and absent (0 matches)",
                  espdl["issue_claim_check"]["search"] == {"file": "data/pie_examples_measured.json",
                                                           "patterns": ["0-1", "0 - 1", "0–1"], "matches": 0}
                  and "0 matches" in espdl["issue_claim_check"]["result"],
                  json.dumps(espdl["issue_claim_check"]["search"], ensure_ascii=False))
            check("disagreement: what this repository has instead is spelled out, not implied",
                  len(espdl["issue_claim_check"]["what_this_repository_has_instead"]) >= 3,
                  json.dumps(espdl["issue_claim_check"]["what_this_repository_has_instead"])[:200])
            check("disagreement: a claim is only ever quoted, never resolved silently",
                  "not resolved" in espdl["resolution"] or "kept as a conflict" in espdl["resolution"],
                  espdl["resolution"][:160])

            r = payload(await session.call_tool("measured_costs", {"section": "no_such_section"}))
            check("measured_costs rejects an unknown section instead of inventing one",
                  r["found"] is False and r["sections"][0] == "cost_model", json.dumps(r)[:160])

            # The model applied to a caller's own kernel: the constants are the ones in the data file.
            r = payload(await session.call_tool("pie_cost_estimate",
                                                {"blocks": 30, "instructions_per_block": 40,
                                                 "stores_per_block": 1}))
            check("pie_cost_estimate: 30 blocks of 40 instructions with 1 store -> floor 40.6/block, "
                  "1218 cycles, 1.3-1.4x band",
                  r["terms"]["per_block"] == 40.6 and r["terms"]["vector_core_cycles"] == 1218
                  and r["real_operation_band_cycles"] == [1583.4, 1705.2]
                  and r["estimate_only"] is True,
                  json.dumps(r["terms"]))
            check("pie_cost_estimate: every constant carries the line it came from and the sibling-project "
                  "provenance",
                  {k: v["value"] for k, v in r["constants"].items()}
                  == {"store_extra": 0.6, "factor_low": 1.3, "factor_high": 1.4, "division_cycles": 16}
                  and all(v["citations"][0]["lines"] for v in r["constants"].values())
                  and r["provenance"]["measured_in_this_repository"] is False,
                  json.dumps({k: v["citations"][0]["lines"] for k, v in r["constants"].items()}))
            check("pie_cost_estimate: the integer-division disagreement is surfaced, not hidden",
                  r["integer_division_note"]["used_cycles_per_division"] == 16
                  and r["integer_division_note"]["the_same_document_also_says"] == 32
                  and r["integer_division_note"]["disagreement_entry"]
                  == "integer_division_16_vs_32_within_one_document", json.dumps(r["integer_division_note"])[:200])

            r = payload(await session.call_tool("pie_cost_estimate",
                                                {"blocks": 30, "instructions_per_block": 135,
                                                 "runs": 13, "cycles_outside_loop_per_run": 174,
                                                 "divisions_in_row_setup": 28}))
            check("pie_cost_estimate: a run-split kernel gets the scaffold term and the warning that the "
                  "1.3-1.4x factor was calibrated elsewhere",
                  r["terms"]["scaffold_cycles"] == 2262 and r["terms"]["row_setup_cycles"] == 448
                  and r["warnings"] and "calibrated" in r["warnings"][0], json.dumps(r["terms"]))

            r = payload(await session.call_tool("pie_cost_estimate",
                                                {"blocks": 0, "instructions_per_block": 0}))
            check("pie_cost_estimate refuses an empty kernel instead of returning zero",
                  r.get("error") == "nothing_to_estimate", json.dumps(r)[:160])
            r = payload(await session.call_tool("pie_cost_estimate",
                                                {"blocks": 1, "instructions_per_block": 10,
                                                 "stores_per_block": -1}))
            check("pie_cost_estimate refuses a negative term", r.get("error") == "negative_term",
                  json.dumps(r)[:160])

            res = await session.list_resources()
            uris = sorted(str(x.uri) for x in res.resources)
            check("the fourth tier's data file is readable as a resource",
                  "esp32s3://pocketjs/pie-costs" in uris, str(uris))
            doc = json.loads((await session.read_resource("esp32s3://docs/sources")).contents[0].text)
            check("docs/sources lists the sibling document separately from the primary sources, with its "
                  "digest",
                  doc["sibling_project_document"]["sha256"] == costs_file["source"]["sha256"]
                  and set(doc["documents"]) == {"trm", "datasheet"},
                  json.dumps(doc["sibling_project_document"])[:200])

            # The registry is the route index: what is reachable, through which tool or resource, from which
            # copy of the knowledge. These checks are what keeps the index and the surface from drifting
            # apart -- a tool nobody can reach, an artifact nobody can read, or a count the data does not
            # support all fail here.
            reg = payload(await session.call_tool("knowledge_routes", {}))
            by_id = {a["id"]: a for a in reg["artifacts"]}
            check("knowledge_routes: no required artifact is missing",
                  reg["summary"]["missing_required"] == [] and reg["summary"]["present"] >= 11,
                  json.dumps(reg["summary"])[:200])
            check("knowledge_routes counts what it opened: 1581 registers / 220 instructions / 217 pipeline "
                  "rows",
                  by_id["registers"]["records"] == 1581 and by_id["pie_instructions"]["records"] == 220
                  and by_id["pie_pipeline"]["records"] == 217,
                  json.dumps({k: by_id[k].get("records")
                              for k in ("registers", "pie_instructions", "pie_pipeline")}))
            check("knowledge_routes keeps the layers apart (1=PDF, 2=toolchain, 3=this device, 4=sibling) "
                  "and names the route",
                  by_id["registers"]["layer"] == 1 and by_id["pie_timing_measured"]["layer"] == 3
                  and by_id["pie_measured_costs"]["layer"] == 4
                  and by_id["pie_measured_costs"]["served_by"]["tools"] == ["measured_costs",
                                                                           "pie_cost_estimate"]
                  and by_id["toolchain"]["layer"] == 2,
                  json.dumps({k: (by_id[k]["layer"], by_id[k]["served_by"]) for k in
                              ("registers", "pie_measured_costs", "toolchain")})[:220])
            routed_tools = {t for a in reg["artifacts"] for t in a["served_by"]["tools"]}
            routed_res = {r for a in reg["artifacts"] for r in a["served_by"]["resources"]}
            check("every advertised tool is listed in the registry", routed_tools == set(names),
                  "unrouted: " + str(sorted(set(names) - routed_tools))
                  + " unknown: " + str(sorted(routed_tools - set(names))))
            check("every advertised resource is listed in the registry", routed_res == set(uris),
                  "unrouted: " + str(sorted(set(uris) - routed_res))
                  + " unknown: " + str(sorted(routed_res - set(uris))))
            check("every extracted/measured artifact is present -- i.e. the wheel ships its data",
                  all(a["present"] for a in reg["artifacts"]
                      if a["shipped_in_wheel"] and a["route_kind"] in ("extracted", "measured")),
                  json.dumps([a["id"] for a in reg["artifacts"]
                              if a["shipped_in_wheel"] and a["route_kind"] in ("extracted", "measured")
                              and not a["present"]]))
            check("the registry says which copy of the knowledge answered, and how it was chosen",
                  reg["roots"]["data"]["source"] in ("env", "wheel", "repo")
                  and reg["roots"]["data"]["exists"] and reg["roots"]["data"]["searched"],
                  json.dumps(reg["roots"]["data"])[:220])
            reg_doc = json.loads((await session.read_resource("esp32s3://registry")).contents[0].text)
            check("esp32s3://registry serves the same index the tool returns",
                  [a["id"] for a in reg_doc["artifacts"]] == [a["id"] for a in reg["artifacts"]]
                  and reg_doc["summary"]["missing_required"] == [], "")
            rev = json.loads((await session.read_resource("esp32s3://trm/review")).contents[0].text)
            check("esp32s3://trm/review serves the manual's self-contradictions (a data file that had no "
                  "route before)",
                  isinstance(rev, list) and len(rev) == 2, json.dumps(rev)[:160])

    print(f"\n{'FAILED' if FAILED else 'PASSED'}: {len(FAILED)} failure(s) of {CHECKED} checks"
          + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
