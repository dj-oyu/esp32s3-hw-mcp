#!/usr/bin/env python3
"""ESP32-S3 hardware knowledge MCP server (stdio).

Serves three layers, kept apart on purpose:

1. knowledge extracted from Espressif's own PDFs by tools/extract_*.py -- every reply carries the document,
   its version and the printed page, so a caller can check the claim instead of trusting it;
2. what the toolchain actually encodes (tools/asm_toolchain.py): the same Espressif binutils that builds
   firmware assembles a snippet or a reconstructed instruction word, so "what does this mnemonic encode to"
   is answered by the assembler, not by a reading of the manual;
3. what the device did (data/pie_timing_measured.json, produced by experiments/pie-timing on a real
   ESP32-S3) and where the manual and the assembler disagree (data/pie_encoding_errata.json).

The manual's full text is *not* in this repository (only the extracted facts are), so `search_manual` and
`get_page` need a local corpus:

    bash tools/fetch_sources.sh
    .venv/bin/python tools/build_corpus.py sources/esp32-s3_technical_reference_manual_en.pdf --out corpus/trm-s3

Run:
    .venv/bin/python server/esp32s3_mcp.py            # MCP over stdio
    .venv/bin/python server/esp32s3_mcp.py --list     # print the tool surface and exit (for humans)
"""
from __future__ import annotations

import json
import os
import re
import sys
from functools import lru_cache

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("ESP32S3_DATA_DIR", os.path.join(ROOT, "data"))
CORPUS = os.environ.get("ESP32S3_CORPUS_DIR", os.path.join(ROOT, "corpus"))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
try:                                        # the toolchain bridge is optional: it needs Espressif binutils
    import asm_toolchain                     # noqa: E402
except Exception as _exc:                    # pragma: no cover - environment
    asm_toolchain = None
    _TOOLCHAIN_IMPORT_ERROR = str(_exc)

# The documents the data was extracted from. Digests match tools/fetch_sources.sh, so a caller can tell
# whether a claim was built from the revision it has.
DOCS = {
    "trm": {"document": "ESP32-S3 Technical Reference Manual", "version": "1.8", "pages": 1531,
            "url": "https://documentation.espressif.com/esp32-s3_technical_reference_manual_en.pdf",
            "sha256": "4484bf8a69035ec42a731c58c64ada6fbd1f1618c5559409f134d9ea083f444f"},
    "datasheet": {"document": "ESP32-S3 Series Datasheet", "version": "2.2", "pages": 87,
                  "url": "https://documentation.espressif.com/esp32-s3_datasheet_en.pdf",
                  "sha256": "2d5a7cb7fd559d8d972bd88db32669c0196d23f22d7afaafb0f63d099b589a3f"},
}


def cite(doc: str, page: int, what: str = "") -> dict:
    """A citation is never optional: this is what makes an answer checkable."""
    d = DOCS[doc]
    return {"document": d["document"], "version": d["version"], "page": page, "what": what,
            "url": d["url"], "doc_sha256": d["sha256"]}


@lru_cache(maxsize=1)
def registers() -> list[dict]:
    return json.load(open(os.path.join(DATA, "registers.json"), encoding="utf-8"))


@lru_cache(maxsize=1)
def peripherals() -> list[dict]:
    return json.load(open(os.path.join(DATA, "peripheral_map.json"), encoding="utf-8"))


@lru_cache(maxsize=1)
def instructions() -> list[dict]:
    return json.load(open(os.path.join(DATA, "pie_instructions.json"), encoding="utf-8"))


@lru_cache(maxsize=1)
def pipelines() -> list[dict]:
    return json.load(open(os.path.join(DATA, "pie_pipeline.json"), encoding="utf-8"))


@lru_cache(maxsize=1)
def pages() -> list[dict] | None:
    path = os.path.join(CORPUS, "trm-s3", "pages.jsonl")
    if not os.path.exists(path):
        return None
    return [json.loads(line) for line in open(path, encoding="utf-8")]


@lru_cache(maxsize=1)
def measured() -> dict | None:
    """Timings measured on a real ESP32-S3 by experiments/pie-timing (see notes/04)."""
    path = os.path.join(DATA, "pie_timing_measured.json")
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


@lru_cache(maxsize=1)
def errata() -> dict | None:
    """Instructions where the manual's printed diagram and the Espressif assembler disagree."""
    path = os.path.join(DATA, "pie_encoding_errata.json")
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


CORPUS_HINT = ("The manual's page text is not part of this repository. Build it once:\n"
               "  bash tools/fetch_sources.sh\n"
               "  .venv/bin/python tools/build_corpus.py sources/esp32-s3_technical_reference_manual_en.pdf"
               " --out corpus/trm-s3")


def norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def find_registers(query: str, limit: int = 20) -> list[dict]:
    """Registers whose name contains the query (case/underscore-insensitive), exact matches first."""
    q = norm(query)
    if not q:
        return []
    exact = [r for r in registers() if norm(r["name"]) == q]
    if not exact and not query.endswith("_REG"):
        exact = [r for r in registers() if norm(r["name"]) == norm(query + "_REG")]
    partial = [r for r in registers() if q in norm(r["name"])]
    out, seen = [], set()
    for r in exact + partial:
        key = r["name"]
        if key not in seen:
            seen.add(key)
            out.append(r)
        if len(out) >= limit:
            break
    return out


def register_reply(r: dict) -> dict:
    out = {
        "name": r["name"],
        "offset": r["offset"],
        "offset_expr": r["offset_expr"],
        "offset_is_parameterised": r["offset_is_parameterised"],
        "address_is_absolute": r["address_is_absolute"],
        "access": r["access"],
        "description": r["description"],
        "group": r["group"],
        "section": r["section"],
        "chapter": r["chapter"],
        "citation": cite("trm", r["source_page"], f"{r['section']} — {r['name']}"),
    }
    if r["address_is_absolute"]:
        out["address_note"] = "This chapter prints the full address (not an offset from a peripheral base)."
    return out


def guess_base(r: dict) -> dict | None:
    """Heuristic peripheral base for a register offset: family prefix vs Table 4.3-3 targets.

    Deliberately reported as a guess (score + candidate list) — the mapping from register prefix to
    peripheral instance is not stated in the manual, so a caller must confirm it on hardware or in ESP-IDF.
    """
    family = r["name"].split("_")[0]
    cands = []
    for p in peripherals():
        target = p["target"].upper()
        if not target or target == "RESERVED":
            continue
        if family in norm(target) or norm(target).startswith(family):
            cands.append(p)
    if not cands:
        return None
    return {"matched_peripheral": cands[0]["target"], "low_address": cands[0]["low_address"],
            "high_address": cands[0]["high_address"],
            "citation": cite("trm", cands[0]["source_page"], "Table 4.3-3 Module/Peripheral Address Mapping"),
            "confidence": "heuristic (register-name prefix matched to a target in Table 4.3-3)",
            "other_candidates": [c["target"] for c in cands[1:4]]}


def pie_lookup(name: str) -> tuple[dict | None, str]:
    """(pipeline row, status) for one instruction name.

    status: "ok" | "no_primary_source" (1.8 documents it, Table 1.7-2 does not) | "not_in_table" |
    "not_a_pie_instruction" (native Xtensa, whose timing the TRM does not state).
    """
    key = name.strip().upper().split()[0] if name.strip() else ""
    row = next((p for p in pipelines() if p["instruction"] == key), None)
    if row:
        return row, "ok"
    if key in {i["name"] for i in instructions()}:
        return None, "no_primary_source"
    return None, "not_a_pie_instruction" if not key.startswith("EE.") else "not_in_table"


def unit_of(seq: str) -> list[str]:
    """Instruction names of a sequence, tolerating 'EE.ANDQ qa, qx, qy' entries."""
    out = []
    for item in seq:
        name = item.strip().upper().split()[0] if item.strip() else ""
        if name:
            out.append(name)
    return out


PIPELINE_RULE = {
    "formula": "D = max(SA - SB + 1, 0)",
    "reading": "SA is the pipeline stage at which the producing instruction writes its result, SB the stage "
               "at which the consuming instruction reads it (TRM 1.7.1, p65). D is the minimum issue "
               "distance in cycles; the interlock (stall) is D - 1 = max(SA - SB, 0).",
    "stage_numbers": {"1": "E (execute)", "2": "M (memory access)"},
    "citation": cite("trm", 65, "1.7.1 Data Hazard"),
    "manual_inconsistency": "The worked example on p65 computes D = max(2-1+1,0) = 2 for SA = W, i.e. it "
                            "treats W as stage 2, while Table 1.7-1 numbers W as 3. Table 1.7-2's cells only "
                            "ever use 1 (E) and 2 (M) for both use and def, so the discrepancy does not reach "
                            "this computation -- but it is unresolved in the manual.",
}


def base_server_helpers() -> None:
    return None


def build_server():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="esp32s3-hw",
        title="ESP32-S3 hardware knowledge (Espressif TRM/Datasheet)",
        version="0.1.0",
        instructions=(
            "Answers about the ESP32-S3, from three layers that are deliberately kept apart. (1) "
            "Espressif's own documents (TRM v1.8, Datasheet v2.2): register maps, PIE instructions, "
            "pipeline stages, addresses — every reply carries the printed page it came from, so quote "
            "that page when you pass an answer on. (2) The toolchain: check_asm, instruction_encoding "
            "and decode_instruction run the same Espressif binutils that builds firmware, so what a "
            "mnemonic encodes to is reported from the assembler rather than from a reading of the "
            "manual. (3) The device: measured_timing serves timings measured on real silicon, and "
            "manual_errata lists the instructions where the manual and the assembler disagree. Facts "
            "that no layer states (e.g. base addresses for a register family, field bit ranges) are "
            "reported as absent or heuristic, never invented."),
    )

    @server.tool(description="Look up an ESP32-S3 register by name (exact, or substring matches).")
    def get_register(name: str, include_base_guess: bool = False, limit: int = 10) -> dict:
        hits = find_registers(name, limit=limit)
        if not hits:
            return {"found": False, "query": name,
                    "hint": "Register names end in _REG, e.g. GDMA_IN_CONF0_CH0_REG. "
                            "Use list_registers(prefix=...) to browse a peripheral."}
        out = []
        for r in hits:
            item = register_reply(r)
            if include_base_guess and not r["address_is_absolute"]:
                item["base_guess"] = guess_base(r)
            out.append(item)
        return {"found": True, "count": len(out), "registers": out}

    @server.tool(description="List registers by name prefix, chapter, section or group label.")
    def list_registers(prefix: str = "", chapter: int = 0, section: str = "",
                       group: str = "", limit: int = 50, offset: int = 0) -> dict:
        rows = registers()
        if prefix:
            p = norm(prefix)
            rows = [r for r in rows if norm(r["name"]).startswith(p)]
        if chapter:
            rows = [r for r in rows if r["chapter"] == chapter]
        if section:
            s = " ".join(section.split()).lower()
            rows = [r for r in rows if s in r["section"].lower()]
        if group:
            g = group.lower()
            rows = [r for r in rows if (r["group"] or "").lower().find(g) >= 0]
        total = len(rows)
        window = rows[offset:offset + max(1, min(limit, 200))]
        return {"total_matching": total, "returned": len(window), "offset": offset,
                "sections": sorted({r["section"] for r in rows}),
                "registers": [{"name": r["name"], "offset": r["offset"], "offset_expr": r["offset_expr"],
                               "access": r["access"], "group": r["group"], "section": r["section"],
                               "page": r["source_page"]} for r in window]}

    @server.tool(description="Look up a PIE extended instruction (EE.*) — encoding, syntax, operation.")
    def get_instruction(name: str, limit: int = 5) -> dict:
        q = name.upper().strip()
        hits = [i for i in instructions() if i["name"] == q]
        if not hits:
            hits = [i for i in instructions() if q in i["name"]][:limit]
        if not hits:
            return {"found": False, "query": name,
                    "hint": "PIE instructions are named EE.<MNEMONIC>[.<SUFFIX>], e.g. EE.VMULAS.S16.QACC."}
        return {"found": True, "count": len(hits), "instructions": [{
            "name": i["name"],
            "instruction_word": i["instruction_word"],
            "assembler_syntax": i["assembler_syntax"],
            "description": i["description"],
            "operation": i["operation"],
            "citation": cite("trm", i["source_page"], f"1.8 {i['name']} (functional description)"),
        } for i in hits]}

    @server.tool(description="Pipeline staging (use/def stages) and hazard facts for a PIE instruction. "
                             "When Table 1.7-2 does not list the instruction, any staging measured on real "
                             "silicon is reported separately, labelled as measured rather than tabulated.")
    def instruction_pipeline(name: str) -> dict:
        q = name.upper().strip()
        row = next((p for p in pipelines() if p["instruction"] == q), None)
        if row is None:
            row = next((p for p in pipelines() if q in p["instruction"]), None)

        # Measured staging for the three instructions the table omits (LD.QR / ST.QR / MV.QR), only ever
        # from a run that passed its own validity gate, and never mixed into the table's numbers.
        measured_rows = []
        m = measured() or {}
        if m.get("valid"):
            for d in m.get("derived", []):
                if q and (q in d.get("instruction", "").upper()):
                    measured_rows.append({"instruction": d["instruction"], "attribute": d["attribute"],
                                          "stage": d["stage"], "basis": d.get("basis"),
                                          "citation": d.get("citation"),
                                          "provenance": {"kind": "measured_on_hardware",
                                                         "log": (m.get("provenance") or {}).get("log")}})
        if row is None:
            out = {"found": False, "query": name,
                   "hint": "Table 1.7-2 covers 217 of the 220 instructions; LD.QR/ST.QR/MV.QR (p301-303) "
                           "are absent from it, so their staging has no primary source."}
            if measured_rows:
                out["measured"] = measured_rows
                out["hint"] += " A run on real silicon did pass its anchors for these, so `measured` is " \
                               "what they are -- quote it as a measurement, not as the manual."
            return out
        stages = {"1": "E (execute)", "2": "M (memory access)"}
        out = {
            "found": True,
            "instruction": row["instruction"],
            "operands_use": row["operands_use"], "operands_def": row["operands_def"],
            "special_regs_use": row["special_regs_use"], "special_regs_def": row["special_regs_def"],
            "raw_cells": {k: row[k + "_text"] for k in
                          ("operands_use", "operands_def", "special_regs_use", "special_regs_def")},
            "stage_meaning": stages,
            "citation": cite("trm", row["source_page"], "Table 1.7-2 Extended Instruction Pipeline Stages"),
            "hazard_rules": "TRM 1.7.1 data hazard (D = max(SA - SB + 1, 0)), 1.7.2 hardware resource "
                            "hazard, 1.7.3 control hazard (2-cycle branch penalty) — pages 65-75.",
        }
        if measured_rows:
            out["measured"] = measured_rows
        return out

    @server.tool(description="Peripheral address ranges (Table 4.3-3) used to turn register offsets "
                             "into absolute addresses.")
    def list_peripherals(target: str = "") -> dict:
        rows = peripherals()
        if target:
            t = target.lower()
            rows = [p for p in rows if t in (p["target"] or "").lower()]
        return {"count": len(rows), "peripherals": [{
            **{k: p[k] for k in ("target", "low_address", "high_address", "size_kb", "notes")},
            "citation": cite("trm", p["source_page"], "Table 4.3-3 Module/Peripheral Address Mapping"),
        } for p in rows]}

    @server.tool(description="Substring search over the TRM's page text (needs a local corpus).")
    def search_manual(query: str, limit: int = 10, context_chars: int = 160) -> dict:
        ps = pages()
        if ps is None:
            return {"error": "corpus_missing", "hint": CORPUS_HINT}
        needle = query.lower()
        hits, total = [], 0
        for page in ps:
            text = page["text"]
            low = text.lower()
            start = low.find(needle)
            if start < 0:
                continue
            total += 1
            if len(hits) < limit:
                excerpt = text[max(0, start - context_chars // 2):start + context_chars // 2]
                hits.append({"page": page["pdf_page"], "excerpt": " ".join(excerpt.split()),
                             "citation": cite("trm", page["pdf_page"], f"match for {query!r}")})
        return {"query": query, "pages_with_match": total, "returned": len(hits), "hits": hits}

    @server.tool(description="Read one printed page of the TRM verbatim (needs a local corpus).")
    def get_page(page: int) -> dict:
        ps = pages()
        if ps is None:
            return {"error": "corpus_missing", "hint": CORPUS_HINT}
        if not 1 <= page <= len(ps):
            return {"error": "page_out_of_range", "pages": len(ps)}
        return {"page": page, "text": ps[page - 1]["text"], "citation": cite("trm", page)}

    @server.tool(description="Estimate interlock (stall) cycles for a sequence of PIE instructions, using "
                              "TRM Table 1.7-2 stages and the 1.7.1 rule. Models adjacent-pair data hazards "
                              "only; resource/control hazards are reported as unmodelled.")
    def analyze_sequence(instructions: list[str], include_pairs: bool = True) -> dict:
        names = unit_of(instructions)
        if not names:
            return {"error": "empty_sequence"}
        records, unmodelled_seen = [], []
        for name in names:
            row, status = pie_lookup(name)
            rec = {"instruction": name, "status": status}
            if row:
                rec.update({
                    "operands_use": row["operands_use"], "operands_def": row["operands_def"],
                    "special_regs_use": row["special_regs_use"], "special_regs_def": row["special_regs_def"],
                    "citation": cite("trm", row["source_page"],
                                     f"Table 1.7-2 row for {row['instruction']}"),
                })
            elif status == "no_primary_source":
                rec["note"] = ("Table 1.7-2 does not list this instruction, so its operand staging has no "
                               "primary source and it is excluded from the cycle estimate.")
                rec["citation"] = cite("trm", 301, "1.8.218-1.8.220 (LD.QR/ST.QR/MV.QR, p301-303)")
                unmodelled_seen.append(f"{name}: absent from Table 1.7-2")
            else:
                rec["note"] = ("Not a PIE extended instruction, so the TRM gives no staging for it; its own "
                               "timing is not modelled here (the TRM does not define the base Xtensa ISA).")
                unmodelled_seen.append(f"{name}: not a PIE instruction")
            records.append(rec)

        pairs, total = [], 0
        for a, b in zip(records, records[1:]):
            if not (a.get("operands_use") is not None and b.get("operands_use") is not None):
                pairs.append({"from": a["instruction"], "to": b["instruction"], "stall_cycles": None,
                              "reason": "one of the two has no tabulated staging"})
                continue
            defs = {o["reg"]: o["stage"] for o in a["operands_def"] + a["special_regs_def"]
                    if o.get("reg") and o.get("stage")}
            uses = {o["reg"]: o["stage"] for o in b["operands_use"] + b["special_regs_use"]
                    if o.get("reg") and o.get("stage")}
            conflicts = []
            for reg, sa in defs.items():
                sb = uses.get(reg)
                if sb is None:
                    continue
                d = max(sa - sb + 1, 0)
                conflicts.append({"register": reg, "producer_def_stage": sa, "consumer_use_stage": sb,
                                  "min_issue_distance": d, "stall_cycles": max(sa - sb, 0)})
            conflicts.sort(key=lambda c: (-c["stall_cycles"], c["register"]))
            stall = max([c["stall_cycles"] for c in conflicts], default=0)
            total += stall
            pairs.append({"from": a["instruction"], "to": b["instruction"], "stall_cycles": stall,
                          "conflicts": conflicts})
        if not include_pairs:
            for p in pairs:
                p.pop("conflicts", None)

        return {
            "sequence": names,
            "stall_cycles_total": total,
            "issue_cycles_estimate": len(names) + total,
            "records": records,
            "pairs": pairs,
            "rule": PIPELINE_RULE,
            "unmodelled": {
                "items": unmodelled_seen,
                "hardware_resource_hazard": "TRM 1.7.2 delays an instruction that collides on a shared "
                                            "resource (e.g. the eight 16-bit multipliers). Per-instruction "
                                            "resource occupancy is not tabulated, so this is not computed.",
                "hardware_resource_citation": cite("trm", 74, "1.7.2 Hardware Resource Hazard"),
                "control_hazard": "A taken branch discards the R and E stage instructions (2 cycles). PIE has "
                                  "no branches, so this applies only when native Xtensa branches are in the "
                                  "sequence, which are outside this model.",
                "control_hazard_citation": cite("trm", 74, "1.7.3 Control Hazard"),
                "model_scope": "Adjacent-pair data hazards, first-order: each pair's interlock is counted "
                               "independently and longer dependence chains are not simulated. Sub-register "
                               "names are matched exactly as tabulated (qz1/fu0.. are distinct operands).",
            },
        }

    # ---------------------------------------------------------------------------------------------
    # Toolchain-backed verification. The manual's diagrams are read by tools/extract_pie.py, and the
    # same diagrams are what firmware gets generated from -- so every claim they carry is checked
    # against the Espressif assembler here rather than trusted. These tools answer "what does this
    # actually encode to on the toolchain that builds the firmware?".
    # ---------------------------------------------------------------------------------------------

    def toolchain_guard() -> dict:
        if asm_toolchain is None:
            return {"error": "toolchain_bridge_unavailable",
                    "detail": globals().get("_TOOLCHAIN_IMPORT_ERROR")}
        info = asm_toolchain.toolchain_info()
        if not info.get("available"):
            return {"error": "toolchain_missing", **info}
        return {}

    @server.tool(description="Assemble Xtensa/PIE assembly with the Espressif binutils that builds the "
                             "firmware and report what it encoded. expected_words, if given, is compared "
                             "with the encodings in order, so an encoding claim is checked, not believed.")
    def check_asm(snippet: str, expected_words: list[str] | None = None, raw: bool = False) -> dict:
        guard = toolchain_guard()
        if guard:
            return guard
        assert asm_toolchain is not None
        result = asm_toolchain.assemble(snippet, raw=raw)
        if expected_words:
            body = result.get("body_instructions") or []
            checks = [{"expected": want, "actual": body[i]["word"] if i < len(body) else None,
                       "assembly_source": body[i]["source"] if i < len(body) else None}
                      for i, want in enumerate(expected_words)]
            for c in checks:
                c["match"] = bool(c["actual"]) and c["expected"].lower().lstrip("0x") == c["actual"].lower()
            result["expected_word_checks"] = checks
            result["all_expected_matched"] = bool(checks) and all(c["match"] for c in checks)
        result.pop("source", None)          # the generated wrapper is noise for a caller
        return result

    @server.tool(description="Turn an instruction word (hex, most significant bit first) back into a "
                             "mnemonic with the disassembler.")
    def decode_instruction(word: str) -> dict:
        guard = toolchain_guard()
        if guard:
            return guard
        assert asm_toolchain is not None
        return asm_toolchain.decode(word)

    @server.tool(description="Check one PIE instruction's encoding: substitute operands into the manual's "
                             "own instruction-word diagram and compare with what the assembler emits. "
                             "Reports the first differing bit when they disagree.")
    def instruction_encoding(name: str) -> dict:
        guard = toolchain_guard()
        if guard:
            return guard
        assert asm_toolchain is not None
        table = {i["name"]: i for i in instructions()}
        key = name.upper().strip()
        if key not in table:
            near = [n for n in table if key in n][:8]
            return {"found": False, "query": name, "near_matches": near}
        out = asm_toolchain.check_instruction(table[key])
        out["found"] = True
        out["manual_instruction_word"] = table[key]["instruction_word"]
        return out

    @server.tool(description="Which assembler/objdump backs the encoding checks, and their version. "
                             "Absent toolchain means the encoding tools report that instead of guessing.")
    def toolchain_status() -> dict:
        if asm_toolchain is None:
            return {"available": False, "error": "toolchain_bridge_unavailable",
                    "detail": globals().get("_TOOLCHAIN_IMPORT_ERROR")}
        return asm_toolchain.toolchain_info()

    @server.tool(description="Timings measured on real ESP32-S3 silicon (experiments/pie-timing), not "
                             "manual text: includes whether the run passed its own validity gate, the "
                             "anchors that test the method, and the caveats.")
    def measured_timing(instruction: str = "") -> dict:
        m = measured()
        if m is None:
            return {"error": "no_measurements",
                    "hint": "Run experiments/pie-timing on a device (tools/device_experiment.sh); the "
                            "result lands in data/pie_timing_measured.json."}
        q = instruction.upper().strip()
        rows = m.get("measurements", [])
        derived = m.get("derived", [])
        if q:
            rows = [r for r in rows if q in r["case"].upper()]
            derived = [d for d in derived if q in d["instruction"].upper()]
        out = {"valid": m.get("valid"), "provenance": m.get("provenance"), "anchors": m.get("anchors"),
               "derived": derived, "measurements": rows, "caveats": m.get("caveats"),
               "problems": m.get("problems")}
        if not m.get("valid"):
            out["warning"] = ("This run did not pass its validity gate, so no derived stage numbers are "
                              "offered. The anchors and the measured stalls are still reported, because "
                              "they are what says the method itself is not yet trustworthy.")
        return out

    @server.tool(description="Instructions where the manual's printed instruction-word diagram and the "
                             "Espressif assembler disagree, with the evidence for each. Generated by "
                             "tools/asm_toolchain.py --errata over every PIE instruction.")
    def manual_errata(instruction: str = "") -> dict:
        e = errata()
        if e is None:
            return {"error": "no_errata_file",
                    "hint": "Regenerate with: .venv/bin/python tools/asm_toolchain.py --errata "
                            "data/pie_encoding_errata.json"}
        q = instruction.upper().strip()
        items = e.get("instructions", [])
        if q:
            items = [i for i in items if q in i["instruction"].upper()]
        return {"provenance": e.get("provenance"), "summary": e.get("summary"),
                "instructions": items,
                "how_to_read": "status=assembler_rejected: the manual's own syntax line cannot be assembled "
                               "with the documented operand; mismatch: the diagram's bits do not reproduce "
                               "the emitted word; syntax_names_other_instruction: the syntax line names a "
                               "different instruction; layout_not_machine_readable: the extracted diagram "
                               "is not a field list, so the extraction needs fixing."}

    @server.resource("esp32s3://trm/pie-hazards", title="TRM 1.7 Instruction Performance (verbatim)",
                     mime_type="text/markdown")
    def pie_hazards() -> str:
        path = os.path.join(DATA, "pie_hazards.md")
        return open(path, encoding="utf-8").read() if os.path.exists(path) else "missing"

    @server.resource("esp32s3://docs/sources", title="Primary sources and their digests",
                     mime_type="application/json")
    def sources() -> str:
        return json.dumps({"note": "Facts are extracted from these documents only; PDFs are not "
                                   "redistributed here (fetch with tools/fetch_sources.sh).",
                           "documents": DOCS}, ensure_ascii=False, indent=1)

    return server


def main() -> int:
    if "--list" in sys.argv:
        server = build_server()
        import asyncio

        tools = asyncio.run(server.list_tools())
        for t in tools:
            print(f"- {t.name}: {(t.description or '').splitlines()[0]}")
        print(f"\ndata dir: {DATA}\ncorpus:   {'present' if pages() is not None else 'absent (search/get_page disabled)'}")
        return 0
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
