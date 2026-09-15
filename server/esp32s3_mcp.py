#!/usr/bin/env python3
"""ESP32-S3 hardware knowledge MCP server (stdio).

Serves four layers, kept apart on purpose:

1. knowledge extracted from Espressif's own PDFs by tools/extract_*.py -- every reply carries the document,
   its version and the printed page, so a caller can check the claim instead of trusting it;
2. what the toolchain actually encodes (tools/asm_toolchain.py): the same Espressif binutils that builds
   firmware assembles a snippet or a reconstructed instruction word, so "what does this mnemonic encode to"
   is answered by the assembler, not by a reading of the manual;
3. what the device did (data/pie_timing_measured.json, produced by experiments/pie-timing on a real
   ESP32-S3; data/pie_examples_measured.json, produced by examples/firmware) and where the manual and the
   assembler disagree (data/pie_encoding_errata.json);
4. what a PIE kernel costs and what breaks when writing one (data/pie_measured_costs.json), measured by the
   *sibling* project cardputer-adv-pocketjs on its own Cardputer ADV and quoted from its docs/pie-simd.md.
   Layer 4 is another project's silicon, not this one's: every reply carries that repository's revision, the
   line range and the verbatim lines, and `measured_in_this_repository: false`. Layers 3 and 4 must never be
   presented as one measurement.

The manual's full text is *not* in this repository (only the extracted facts are), so `search_manual` and
`get_page` need a local corpus. `esp32s3-hw-mcp --fetch-corpus` fetches the pinned PDFs and builds it where
the server looks for it (~/.cache/esp32s3-hw-mcp); from a checkout the same thing is:

    bash tools/fetch_sources.sh
    .venv/bin/python tools/build_corpus.py sources/esp32-s3_technical_reference_manual_en.pdf --out corpus/trm-s3

Run:
    uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp         # MCP over stdio
    uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp --list  # tool surface, for humans
    .venv/bin/python server/esp32s3_mcp.py                                        # same server, from a clone
    .venv/bin/python server/esp32s3_mcp.py --paths                                # which copy answered

Which copy of the knowledge answers is decided in esp32s3_hw_mcp/registry.py -- environment variable, then
the copy inside the installed package (what uvx installs), then the checkout -- and reported by `--paths` and
by the knowledge_routes tool, so an unexpected answer can be traced to the copy that gave it. The registry
also holds the routes: which tool or resource reaches which artifact.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request
from functools import lru_cache

try:                                        # installed (uvx / pip): the package is importable
    from esp32s3_hw_mcp import __version__ as VERSION
    from esp32s3_hw_mcp import registry
except ImportError:                         # run as `python server/esp32s3_mcp.py`: the package is at the root
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from esp32s3_hw_mcp import __version__ as VERSION   # noqa: E402
    from esp32s3_hw_mcp import registry      # noqa: E402

ROOT = registry.REPO_ROOT                      # the checkout, when this is one
TOOLS = os.path.join(ROOT, "tools")
DATA = registry.resolve_data()["path"]
CORPUS = registry.resolve_corpus()["path"]

_TOOLCHAIN_IMPORT_ERROR = ""
try:                                        # the toolchain bridge ships inside the wheel ...
    from esp32s3_hw_mcp import asm_toolchain  # type: ignore[attr-defined]
except Exception:
    asm_toolchain = None                    # type: ignore[assignment]
if asm_toolchain is None and os.path.isdir(TOOLS):   # ... and lives in tools/ in a checkout
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    try:
        import asm_toolchain                 # noqa: E402,F811
    except Exception as _exc:                # pragma: no cover - environment
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


def measured_interlocks() -> list[dict]:
    """Interlocks measured on real silicon, one entry per (producer, consumer) pair the harness ran.

    Only ever from a run that passed its own validity gate. These are measurements of *this* chip, so they
    are handed back in their own field with `provenance.kind = measured_on_hardware` and never merged with
    Table 1.7-2's stage numbers, which stay in `citation` and stay the table's claim. Where the two
    disagree, both are reported: a measured stall that misses the model is a finding about the manual, not
    a reason to hide either number.
    """
    m = measured() or {}
    if not m.get("valid"):
        return []
    prov = {"kind": "measured_on_hardware",
            "log": (m.get("provenance") or {}).get("log"),
            "firmware_git_rev": (m.get("provenance") or {}).get("firmware_git_rev"),
            "firmware_image_sha256": (m.get("provenance") or {}).get("firmware_image_sha256")}
    out = []
    for p in m.get("predictions", []):
        entry = {
            "case": p.get("case"),
            "producer": p.get("producer"), "consumer": p.get("consumer"),
            "registers": p.get("registers"),
            "measured_stall_cycles_at_distance_1": p.get("measured_stall_at_d1"),
            "predicted_stall_cycles_at_distance_1": p.get("predicted_stall_at_d1"),
            "predicted_min_issue_distance_D": p.get("predicted_min_issue_distance_D"),
            "measured_min_issue_distance_D": p.get("measured_min_issue_distance_D"),
            "distances_measured": p.get("distances_measured"),
            "stall_by_distance": p.get("stall_by_distance"),
            "noise_cycles_at_distance_1": p.get("noise_cycles_at_d1"),
            "matches_prediction": p.get("match"),
            "basis": p.get("basis"),
            "prediction_source": p.get("why"),
            "provenance": prov,
        }
        if p.get("confound"):
            entry["confound"] = p["confound"]
        out.append(entry)
    return out


def measured_interlock_for(producer: str, consumer: str) -> dict | None:
    """The measured entry for one instruction pair, matched on mnemonics (operands ignored).

    The measured pairs are (last producer instruction) -> (first consumer instruction), which is the pair
    the harness timed at issue distance 1. Matching the *last* producer instruction matters: a case whose
    producer list is [EE.ZERO.QACC, EE.VMULAS.U16.QACC] timed VMULAS -> consumer, not ZERO.QACC -> consumer.

    Two cases can time the same pair with different independent variants (one of them confounded by a
    different consumer instruction), so an entry whose number carries such an artefact is deprioritised: the
    pair gets the confound-free measurement, and the other one stays reachable through measured_timing().
    """
    p_name = (producer.strip().upper().split() or [""])[0]
    c_name = (consumer.strip().upper().split() or [""])[0]
    if not p_name or not c_name:
        return None
    matches = []
    for entry in measured_interlocks():
        prods = [(x.strip().upper().split() or [""])[0] for x in (entry.get("producer") or [])]
        cons = [(x.strip().upper().split() or [""])[0] for x in (entry.get("consumer") or [])]
        if prods and prods[-1] == p_name and c_name in cons:
            matches.append(entry)
    clean = [x for x in matches if not x.get("confound")]
    return (clean or matches or [None])[0]


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


@lru_cache(maxsize=1)
def measured_semantics() -> dict | None:
    """Semantics measured on a real ESP32-S3 by examples/firmware (see notes/07)."""
    path = os.path.join(DATA, "pie_examples_measured.json")
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


@lru_cache(maxsize=1)
def cost_data() -> dict | None:
    """Costs and pitfalls measured on a real Cardputer ADV by the *sibling* project cardputer-adv-pocketjs.

    Deliberately a different tier from measured() and measured_semantics(): those are this repository's own
    device runs, this one is another project's write-up, quoted with its revision, line range and verbatim
    lines. Every reply built from it says `measured_in_this_repository: false`, because the same author and
    the same class of board is not the same measurement.
    """
    path = os.path.join(DATA, "pie_measured_costs.json")
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


def cost_citation(m: dict, src: dict) -> dict:
    """A citation for the sibling document: line range + verbatim lines instead of a printed page."""
    s = m["source"]
    span = f"{src['line_start']}-{src['line_end']}" if src["line_end"] != src["line_start"] \
        else str(src["line_start"])
    return {
        "document": f"{s['document']} ({s['project']} {s['path']})",
        "version": s["version"],
        "project": s["project"],
        "path": s["path"],
        "lines": span,
        "line_start": src["line_start"],
        "line_end": src["line_end"],
        "what": src.get("what", ""),
        "quote": src["quote"],
        "doc_sha256": s["sha256"],
        "url": s["url"],
        "measured_in_this_repository": False,
        "citation_note": "The primary-source tier cites a printed page because its source is a PDF; this "
                         "tier's source is a Markdown file in another repository, so the line range and the "
                         "verbatim lines are the citation, pinned by sha256.",
    }


def cost_provenance(m: dict, item: dict) -> dict:
    p = m["provenance"]
    return {"kind": item["provenance_kind"], "measured_in_this_repository": False,
            "origin_project": p["origin_project"], "origin_revision": p["origin_revision"],
            "measured_where": p["where"], "estimate": bool(item.get("estimate")),
            "estimate_marker_in_the_source": item.get("estimate_marker"),
            "note": p["note"]}


def cost_item_reply(m: dict, section: str, item: dict) -> dict:
    out = {
        "id": item["id"],
        "section": section,
        "statement": item["statement"],
        "numbers": item["numbers"],
        "how_measured": item["how_measured"],
        "provenance": cost_provenance(m, item),
        "citations": [cost_citation(m, s) for s in item["sources"]],
    }
    if item.get("caveats"):
        out["caveats"] = item["caveats"]
    if item.get("linked_entries"):
        out["linked_entries"] = [{
            "file": lk["file"],
            "locate": lk["locate"],
            **({"expects": lk["expect"]} if lk.get("expect") else {}),
            **({"what": lk["what"]} if lk.get("what") else {}),
            "this_repositorys_own_data": True,
        } for lk in item["linked_entries"]]
        out["linked_entries_note"] = ("These point into this repository's own data files (its TRM extraction "
                                      "and its own device runs). Where an item links one, the fact has been "
                                      "established twice, in different trees -- once here, once by the "
                                      "sibling project. They are kept apart, not merged.")
    if item.get("issue_claim_check"):
        out["issue_claim_check"] = item["issue_claim_check"]
    if item.get("resolution"):
        out["resolution"] = item["resolution"]
    return out


def cost_value(m: dict, item_id: str, as_printed: str) -> tuple[float | None, dict]:
    """(value, the item's reply) for one constant, so a computed answer cites where its constant came from."""
    for sec in m["section_index"]:
        for item in m[sec["name"]]:
            if item["id"] != item_id:
                continue
            for num in item["numbers"]:
                if num["as_printed"] == as_printed:
                    return num.get("value"), cost_item_reply(m, sec["name"], item)
    return None, {}


CORPUS_HINT = ("The manual's page text is not redistributed here, so this needs a corpus. The server can "
               "build one itself, once:\n"
               "  uvx --with pymupdf --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp "
               "--fetch-corpus\n"
               "  (it fetches the sha256-pinned PDFs and leaves the corpus under ~/.cache/esp32s3-hw-mcp, "
               "where the server looks for it)\n"
               "or, from a checkout:\n"
               "  bash tools/fetch_sources.sh\n"
               "  .venv/bin/python tools/build_corpus.py sources/esp32-s3_technical_reference_manual_en.pdf"
               " --out corpus/trm-s3\n"
               "ESP32S3_CORPUS_DIR points the server at a corpus somewhere else.")


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
        version=VERSION,
        instructions=(
            "Answers about the ESP32-S3, from four layers that are deliberately kept apart. (1) "
            "Espressif's own documents (TRM v1.8, Datasheet v2.2): register maps, PIE instructions, "
            "pipeline stages, addresses — every reply carries the printed page it came from, so quote "
            "that page when you pass an answer on. (2) The toolchain: check_asm, instruction_encoding "
            "and decode_instruction run the same Espressif binutils that builds firmware, so what a "
            "mnemonic encodes to is reported from the assembler rather than from a reading of the "
            "manual. (3) The device: measured_timing serves timings measured on real silicon, and "
            "manual_errata lists the instructions where the manual and the assembler disagree. (4) What "
            "a kernel costs and what breaks while writing one: measured_costs and pie_cost_estimate "
            "serve the sibling project cardputer-adv-pocketjs's own measurements of its own kernels "
            "(cost model, pitfalls, the price of a scalar division, measurement discipline), quoted "
            "with that repository's revision, line range and verbatim lines; those replies say "
            "measured_in_this_repository: false, because another project's board is not this "
            "repository's measurement — never merge layer 3 and layer 4 into one number. Facts "
            "that no layer states (e.g. base addresses for a register family, field bit ranges) are "
            "reported as absent or heuristic, never invented. If you want to know what is reachable at all "
            "before asking, call knowledge_routes (or read esp32s3://registry): it lists every artifact, "
            "its layer, and the tool or resource that serves it, with the count it answered from."),
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

        # Interlocks measured on silicon that involve this instruction (issue #1: the pairs built on
        # QACC_H/QACC_L's def->use). Kept in their own field: a measured stall of the *pair* is not a stage
        # of the instruction, and the table's own numbers above are left exactly as tabulated.
        interlocks = [x for x in measured_interlocks()
                      if q and any(q in str(i).upper() for i in (x.get("producer") or [])
                                   + (x.get("consumer") or []))]
        if row is None:
            out = {"found": False, "query": name,
                   "hint": "Table 1.7-2 covers 217 of the 220 instructions; LD.QR/ST.QR/MV.QR (p301-303) "
                           "are absent from it, so their staging has no primary source."}
            if measured_rows:
                out["measured"] = measured_rows
                out["hint"] += " A run on real silicon did pass its anchors for these, so `measured` is " \
                               "what they are -- quote it as a measurement, not as the manual."
            if interlocks:
                out["measured_interlocks"] = interlocks
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
        if interlocks:
            out["measured_interlocks"] = interlocks
            out["measured_interlocks_note"] = (
                "Stalls measured on real silicon for pairs built on this instruction (provenance "
                "`measured_on_hardware`). The tabulated stages above are the manual's claim and are reported "
                "unchanged: where a measurement misses the D = max(SA - SB + 1, 0) prediction, both are kept "
                "so the disagreement stays visible.")
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
                              "only; resource/control hazards are reported as unmodelled. Pairs that were "
                              "measured on real silicon (experiments/pie-timing) come back with the measured "
                              "stall next to the model's, labelled measured_on_hardware.")
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
            pair = {"from": a["instruction"], "to": b["instruction"], "stall_cycles": stall,
                    "conflicts": conflicts}
            # If this exact adjacent pair was run on silicon (experiments/pie-timing), hand the measurement
            # over next to the table's number instead of replacing it: `stall_cycles` stays the model, and
            # `measured_interlock` says what the chip did. A verdict is stated so a caller does not have to
            # diff two numbers of different kinds.
            meas = measured_interlock_for(a["instruction"], b["instruction"])
            if meas:
                pair["measured_interlock"] = {
                    **meas,
                    "table_stall_cycles": stall,
                    "silicon_vs_table": ("agrees"
                                         if abs((meas["measured_stall_cycles_at_distance_1"] or 0.0)
                                                - stall) < 0.25 else "contradicts"),
                }
            pairs.append(pair)
        if not include_pairs:
            for p in pairs:
                p.pop("conflicts", None)

        measured_pairs = [{"from": p["from"], "to": p["to"], **p["measured_interlock"]}
                          for p in pairs if p.get("measured_interlock")]
        out = {
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
        if measured_pairs:
            contradictions = [m for m in measured_pairs if m["silicon_vs_table"] == "contradicts"]
            out["measured_pairs"] = measured_pairs
            out["measured_interlocks_note"] = (
                "`stall_cycles_total` and every `pairs[].stall_cycles` are Table 1.7-2 + rule 1.7.1 -- the "
                "manual's claim, unchanged. `measured_pairs` are stalls of the same adjacent pairs measured "
                "on an ESP32-S3 (provenance `measured_on_hardware`). They are reported side by side and never "
                "merged, so where silicon misses the model the caller sees both rather than one overwritten "
                + ("number. This sequence has %d pair(s) where silicon contradicts the table: %s."
                   % (len(contradictions),
                      ", ".join(f"{m['from']} -> {m['to']} (table {m['table_stall_cycles']}, "
                                f"silicon {m['measured_stall_cycles_at_distance_1']})"
                                for m in contradictions))
                   if contradictions else "number."))
        return out

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
                             "anchors that test the method, the staging the table omits, and one entry per "
                             "instruction pair the harness timed (what Table 1.7-2 + rule 1.7.1 predict "
                             "against what the chip did, with provenance measured_on_hardware).")
    def measured_timing(instruction: str = "") -> dict:
        m = measured()
        if m is None:
            return {"error": "no_measurements",
                    "hint": "Run experiments/pie-timing on a device (tools/device_experiment.sh); the "
                            "result lands in data/pie_timing_measured.json."}
        q = instruction.upper().strip()
        rows = m.get("measurements", [])
        derived = m.get("derived", [])
        interlocks = measured_interlocks()
        if q:
            rows = [r for r in rows if q in r["case"].upper()]
            derived = [d for d in derived if q in d["instruction"].upper()]
            interlocks = [x for x in interlocks
                          if any(q in str(i).upper() for i in (x.get("producer") or [])
                                 + (x.get("consumer") or []))]
        out = {"valid": m.get("valid"), "provenance": m.get("provenance"), "anchors": m.get("anchors"),
               "derived": derived, "measurements": rows, "interlocks": interlocks,
               "predictions": m.get("predictions"), "caveats": m.get("caveats"),
               "problems": m.get("problems")}
        if interlocks:
            out["how_to_read"] = (
                "`derived` is staging the table does not state, derived from a stall difference. `interlocks` "
                "is one entry per instruction pair the harness ran: what Table 1.7-2 + rule 1.7.1 predict "
                "(`predicted_stall_cycles_at_distance_1`, `predicted_min_issue_distance_D`) versus what the "
                "chip did (`measured_stall_cycles_at_distance_1`, `measured_min_issue_distance_D`), with "
                "`matches_prediction` for the verdict and `confound` on a case whose number carries a known "
                "artefact of the measurement design. Everything here is a measurement of one chip at one "
                "clock; the manual's own numbers are never overwritten by it.")
        if not m.get("valid"):
            out["warning"] = ("This run did not pass its validity gate, so no derived stage numbers and no "
                              "measured interlocks are offered. The anchors and the measured stalls are still "
                              "reported, because they are what says the method itself is not yet trustworthy.")
        return out

    @server.tool(description="Instruction semantics measured on real ESP32-S3 silicon with the examples/"
                             "firmware kernels (examples/README.md, notes/07): what the pseudo-code says "
                             "versus what the device did, including the exact lane values, the alignment "
                             "rule, and which manual reading was refuted. Ask about one instruction or "
                             "leave it empty for everything.")
    def example_measured_semantics(instruction: str = "") -> dict:
        m = measured_semantics()
        if m is None:
            return {"error": "no_measured_semantics",
                    "hint": "Build and run examples/firmware (bash tools/build_examples.sh, then "
                            "tools/host_flash_and_log.sh --examples on the host); the findings land in "
                            "data/pie_examples_measured.json."}
        q = instruction.upper().strip()
        hits = m.get("findings", [])
        printed = m.get("printed_not_interpreted", [])
        if q:
            hits = [f for f in hits
                    if q in " ".join(f.get("instructions", [])).upper() or q in f["claim"].upper()]
            printed = [p for p in printed if q in p.get("instruction", "").upper()]
        return {"provenance": m.get("provenance"), "findings": hits,
                "printed_not_interpreted": printed, "open_after_this_run": m.get("open_after_this_run"),
                "how_to_read": "Everything here is measured, so it outranks the manual where the two "
                               "disagree -- but only for the exact configuration that was run. `status` "
                               "says whether the silicon confirmed the claim; `resolves` names the errata "
                               "entry a measurement settles; `printed_not_interpreted` is data whose "
                               "expected value could not be constructed from the documents."}

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

    # ---------------------------------------------------------------------------------------------
    # Fourth tier: what a PIE kernel actually costs and what breaks when writing one, as measured on
    # a real Cardputer ADV by the sibling project cardputer-adv-pocketjs. Quoted, never merged with
    # this repository's own runs: data/pie_measured_costs.json carries the revision, the line range,
    # the verbatim lines and a measured_in_this_repository:false flag on every entry.
    # ---------------------------------------------------------------------------------------------

    def costs_guard() -> dict:
        m = cost_data()
        if m is None:
            return {"error": "no_measured_costs",
                    "hint": "data/pie_measured_costs.json is missing; it is curated from the sibling "
                            "project's docs/pie-simd.md and gated by tools/verify_measured_costs.py."}
        return {}

    @server.tool(description="What PIE instructions really cost, and what breaks silently when writing a "
                             "kernel: the cost model (one cycle per instruction whatever the kind, +0.6 only "
                             "for EE.VST.128.IP, floor = instructions + 0.6 x stores + stalls, 1.3-1.4x in "
                             "real operation), the pitfalls (loopgtz's 256-byte body, early-clobber \"=&a\", "
                             "PIE is coprocessor 3 so no ISRs, no 16-bit lane shift, SRCMB's shift comes from "
                             "AR), the price of scalar divisions and math calls, and what makes a number "
                             "measured on this board untrustworthy. Measured by the SIBLING PROJECT "
                             "cardputer-adv-pocketjs, not by this repository -- every reply marks that and "
                             "cites the document revision, line range and verbatim lines. The section "
                             "index always comes back first; narrow with section= or query=, or ask for "
                             "everything.")
    def measured_costs(section: str = "", query: str = "") -> dict:
        guard = costs_guard()
        if guard:
            return guard
        m = cost_data()
        assert m is not None
        names = [s["name"] for s in m["section_index"]]
        want = section.strip()
        if want and want not in names:
            return {"found": False, "query": section, "sections": names,
                    "hint": "Ask for one of the sections above, or leave the argument empty for the index."}
        q = query.strip().upper()
        selected = [want] if want else names
        items, counts = [], {}
        for name in selected:
            rows = m[name]
            if q:
                rows = [i for i in rows if q in json.dumps(i, ensure_ascii=False).upper()]
            counts[name] = len(rows)
            items.extend(cost_item_reply(m, name, i) for i in rows)
        return {
            "provenance": m["provenance"],
            "document": m["source"],
            "sections": m["section_index"],
            "counts": counts,
            "count": len(items),
            "items": items,
            "read_this_with": m["read_this_with"],
            "how_to_read": "Every item's numbers are the sibling project's own measurements of its own "
                           "kernels, quoted with the lines they came from; `provenance.kind` says whether the "
                           "item is a measurement, a derivation, a static count, an estimate the source "
                           "itself marks as one (`estimate_marker` is the word the source uses), or a "
                           "conflict between sources. `measured_in_this_repository` is false throughout: "
                           "this repository's own device numbers are in measured_timing and "
                           "example_measured_semantics, and the two must not be averaged or quoted as one "
                           "another. Use pie_cost_estimate to apply the model to your own kernel.",
        }

    @server.tool(description="Apply the measured cost model (sibling project cardputer-adv-pocketjs, "
                             "docs/pie-simd.md) to a kernel you are writing: the per-block floor, the "
                             "per-run scaffolding and the row setup, then the 1.3-1.4x real-operation band. "
                             "Returns the arithmetic, the provenance of every constant and what the model "
                             "does not cover. This is an estimate from measured constants, never a "
                             "measurement of your kernel.")
    def pie_cost_estimate(blocks: int, instructions_per_block: int, stores_per_block: int = 0,
                          stalls_per_block: int = 0, runs: int = 0, cycles_outside_loop_per_run: int = 0,
                          divisions_in_row_setup: int = 0) -> dict:
        guard = costs_guard()
        if guard:
            return guard
        m = cost_data()
        assert m is not None
        if blocks < 1 or instructions_per_block < 1:
            return {"error": "nothing_to_estimate",
                    "hint": "blocks and instructions_per_block are the number of loop bodies and the static "
                            "instruction count of one body; both must be positive."}
        bad = {k: v for k, v in (("stores_per_block", stores_per_block), ("stalls_per_block", stalls_per_block),
                                 ("runs", runs), ("cycles_outside_loop_per_run", cycles_outside_loop_per_run),
                                 ("divisions_in_row_setup", divisions_in_row_setup)) if v < 0}
        if bad:
            return {"error": "negative_term", "terms": bad}

        const = {}
        for key, (item_id, printed) in {
            "store_extra": ("lower_bound_formula", "0.6"),
            "factor_low": ("real_operation_is_1_3_to_1_4_times_the_floor", "1.3"),
            "factor_high": ("real_operation_is_1_3_to_1_4_times_the_floor", "1.4"),
            "division_cycles": ("integer_division_about_16_cycles_back_calculated", "16"),
        }.items():
            value, item = cost_value(m, item_id, printed)
            const[key] = {"value": value, "from": item_id, "citations": item.get("citations", [])}
        alt_value, alt_item = cost_value(m, "integer_division_16_vs_32_within_one_document", "32")

        core = blocks * (instructions_per_block + const["store_extra"]["value"] * stores_per_block
                         + stalls_per_block)
        scaffold = runs * cycles_outside_loop_per_run
        setup = divisions_in_row_setup * const["division_cycles"]["value"]
        total = core + scaffold + setup
        lower = round(total * const["factor_low"]["value"], 1)
        upper = round(total * const["factor_high"]["value"], 1)

        out = {
            "estimate_only": True,
            "confidence": "an estimate built from the sibling project's measured constants -- not a "
                          "measurement of your kernel",
            "requested": {"blocks": blocks, "instructions_per_block": instructions_per_block,
                          "stores_per_block": stores_per_block, "stalls_per_block": stalls_per_block,
                          "runs": runs, "cycles_outside_loop_per_run": cycles_outside_loop_per_run,
                          "divisions_in_row_setup": divisions_in_row_setup},
            "terms": {
                "vector_core_cycles": core,
                "per_block": instructions_per_block + const["store_extra"]["value"] * stores_per_block
                             + stalls_per_block,
                "scaffold_cycles": scaffold,
                "row_setup_cycles": setup,
                "total_before_the_real_operation_factor": total,
            },
            "real_operation_band_cycles": [lower, upper],
            "constants": {k: {"value": v["value"], "from": v["from"],
                              "citations": [{"lines": c["lines"], "quote": c["quote"]}
                                            for c in v["citations"]]}
                          for k, v in const.items()},
            "notes": [
                "The floor is per loop body, not per row: count the scaffolding outside loopgtz/bnez with "
                "objdump -d and pass it as runs x cycles_outside_loop_per_run.",
                "0.6 is charged per 128-bit store, so a constant broadcast of nk stores costs nk x 0.6.",
                "The 1.3-1.4x band comes from task switches saving and restoring PIE's coprocessor-3 state; "
                "it is not something the kernel can remove.",
                "Integer divisions are counted here at the measured 16 cycles; the same document's earlier "
                "section says about 32 and is named there as a wrong assumption -- the disagreement entry "
                "carries both.",
            ],
            "citations_for_the_model": cost_item_reply(m, "cost_model", next(
                i for i in m["cost_model"] if i["id"] == "lower_bound_formula"))["citations"],
            "provenance": m["provenance"],
        }
        if runs:
            out["warnings"] = ["The 1.3-1.4x factor was calibrated on kernels where one row is one loop and "
                               "the scaffold is about a tenth of the total; a row split into runs makes the "
                               "scaffold the same order as the vector term, and the document says the factor "
                               "does not hold there."]
        if alt_value is not None:
            out["integer_division_note"] = {
                "used_cycles_per_division": const["division_cycles"]["value"],
                "the_same_document_also_says": alt_value,
                "disagreement_entry": "integer_division_16_vs_32_within_one_document",
                "citations": [{"lines": c["lines"], "quote": c["quote"]} for c in alt_item.get("citations", [])]}
        return out

    @server.resource("esp32s3://pocketjs/pie-costs",
                     title="cardputer-adv-pocketjs: measured PIE costs and pitfalls (verbatim data file)",
                     mime_type="application/json")
    def pocketjs_costs() -> str:
        path = os.path.join(DATA, "pie_measured_costs.json")
        return open(path, encoding="utf-8").read() if os.path.exists(path) else "{}"

    @server.resource("esp32s3://trm/pie-hazards", title="TRM 1.7 Instruction Performance (verbatim)",
                     mime_type="text/markdown")
    def pie_hazards() -> str:
        path = os.path.join(DATA, "pie_hazards.md")
        return open(path, encoding="utf-8").read() if os.path.exists(path) else "missing"

    @server.resource("esp32s3://docs/sources", title="Primary sources and their digests",
                     mime_type="application/json")
    def sources() -> str:
        out = {"note": "Facts are extracted from these documents only; PDFs are not "
                       "redistributed here (fetch with tools/fetch_sources.sh).",
               "documents": DOCS}
        c = cost_data()
        if c:
            out["sibling_project_document"] = {
                "note": "Not a primary source for this repository's extraction and not part of the "
                        "documents above: it is another project's own measurement write-up, quoted by "
                        "measured_costs/pie_cost_estimate with the revision, line range and verbatim lines. "
                        "Every reply from it says measured_in_this_repository: false.",
                **c["source"]}
        return json.dumps(out, ensure_ascii=False, indent=1)

    # ---------------------------------------------------------------------------------------------
    # The route index itself: what is reachable, in which layer, through which tool or resource, and
    # where that copy of the knowledge came from. A caller that gets an unexpected answer (or a
    # `corpus_missing`/`no_measurements` reply) can read this to see whether the route or the install
    # is at fault -- the counts are measured by opening the files, not asserted here.
    # ---------------------------------------------------------------------------------------------

    @server.tool(description="The route index: every artifact this server can answer from, its layer, the "
                             "tool(s) and resource(s) that serve it, its producer and its gate, where each "
                             "root resolved to (environment / installed package / checkout / cache), and how "
                             "many records answered. Call this first to see what is reachable, and to trace "
                             "an unexpected answer to the copy of the knowledge that gave it.")
    def knowledge_routes(include_counts: bool = True) -> dict:
        return registry.describe(with_counts=include_counts)

    @server.resource("esp32s3://registry",
                     title="Knowledge registry: artifacts, layers and the route to each",
                     mime_type="application/json")
    def registry_index() -> str:
        return json.dumps(registry.describe(with_counts=True), ensure_ascii=False, indent=1)

    @server.resource("esp32s3://trm/review", title="TRM rows where the manual disagrees with itself",
                     mime_type="application/json")
    def trm_review() -> str:
        path = os.path.join(DATA, "pie_review.json")
        return open(path, encoding="utf-8").read() if os.path.exists(path) else "[]"

    return server


# ---------------------------------------------------------------------------------------------------------
# Command line. `--fetch-corpus` exists so that the one artifact this repository must not redistribute (the
# manual's page text) is reachable from the same command that serves the knowledge, instead of only from a
# checkout: it fetches the sha256-pinned PDFs, builds the corpus where the server looks for it
# (~/.cache/esp32s3-hw-mcp), and needs pymupdf for that one run.
# ---------------------------------------------------------------------------------------------------------

HELP = """esp32s3-hw-mcp -- ESP32-S3 hardware knowledge server (MCP over stdio)

  (no arguments)            serve MCP over stdio; this is what a client launches
  --list                     the tool surface, for humans
  --paths                    where each root resolved to, and which artifacts are present
  --registry                 the full route index as JSON
  --fetch-corpus             fetch the pinned PDFs and build the page corpus (needs pymupdf)
  --corpus-dir DIR           where --fetch-corpus writes the corpus (default: a checkout's corpus/, else
                             ~/.cache/esp32s3-hw-mcp/corpus)
  --sources-dir DIR          where --fetch-corpus puts the PDFs (same rule)
  --version                  print the version

Environment: ESP32S3_DATA_DIR, ESP32S3_CORPUS_DIR, ESP32S3_SOURCES_DIR, ESP32S3_CACHE_DIR,
             ESP32S3_POCKETJS_DOC. An environment path always wins, and `--paths` shows the search."""


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _opt(args: list[str], name: str, default: str = "") -> str:
    if name not in args:
        return default
    i = args.index(name)
    if i + 1 >= len(args):
        raise SystemExit(f"{name} needs a value")
    return args[i + 1]


def fetch_sources(dest: str) -> dict:
    """Download the two pinned PDFs into dest, checking the digest. A 200 with an HTML body is not a PDF:
    the digest is the test (see sources/SOURCES.md)."""
    os.makedirs(dest, exist_ok=True)
    got = {}
    for key, doc in DOCS.items():
        name = doc["url"].rsplit("/", 1)[-1]
        path = os.path.join(dest, name)
        if os.path.exists(path) and _sha256(path) == doc["sha256"]:
            print(f"  ok (cached)   {name}")
            got[key] = path
            continue
        print(f"  fetching      {name} ({doc['document']} v{doc['version']})")
        req = urllib.request.Request(doc["url"], headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=300) as resp, open(path, "wb") as out:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        digest = _sha256(path)
        if digest != doc["sha256"]:
            raise SystemExit(f"FAIL  {name}: sha256 {digest} != pinned {doc['sha256']} "
                             f"(Espressif re-released the document?)")
        print(f"  ok (fetched)  {name}  {digest[:16]}...")
        got[key] = path
    return got


def _load_corpus_builder():
    """The packaged copy when installed, tools/build_corpus.py in a checkout (same file, two locations)."""
    try:
        from esp32s3_hw_mcp import build_corpus as mod    # type: ignore[attr-defined]
        return mod
    except ImportError:
        pass
    path = os.path.join(TOOLS, "build_corpus.py")
    if not os.path.exists(path):
        raise SystemExit("build_corpus.py not found (neither installed nor in tools/)")
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_corpus", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scratch_dir(dirname: str, env_var: str) -> str:
    env = os.environ.get(env_var, "").strip()
    if env:
        return env
    if os.path.isdir(os.path.join(ROOT, "data")):        # a checkout: keep the repository layout
        return os.path.join(ROOT, dirname)
    return os.path.join(registry.CACHE_DIR, dirname)


def cmd_fetch_corpus(args: list[str]) -> int:
    sources = _opt(args, "--sources-dir") or _scratch_dir("sources", registry.ENV_SOURCES)
    corpus = _opt(args, "--corpus-dir") or _scratch_dir("corpus", registry.ENV_CORPUS)
    print(f"sources -> {sources}\ncorpus  -> {corpus}")
    pdfs = fetch_sources(sources)
    try:
        builder = _load_corpus_builder()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for key, sub in (("trm", "trm-s3"), ("datasheet", "datasheet-s3")):
        out = os.path.join(corpus, sub)
        print(f"building      {out}")
        try:
            builder.build(pdfs[key], out)
        except ImportError as exc:            # pymupdf is needed for this one command
            print(f"{exc}\n\npymupdf is needed only to build the corpus. Run this once as:\n"
                  "  uvx --with pymupdf --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp "
                  "--fetch-corpus", file=sys.stderr)
            return 2
    print("\ndone. `esp32s3-hw-mcp --paths` should now report the corpus as present; search_manual and "
          "get_page answer from it.")
    return 0


def print_paths() -> int:
    reg = registry.describe(with_counts=False)
    print("roots (first hit wins; an ESP32S3_* variable overrides each one):")
    for role, r in reg["roots"].items():
        print(f"  {'ok     ' if r['exists'] else 'MISSING'} {role:<12} {r['path']}")
        print(f"          source: {r['source']} -- {r['how']}")
        for s in r["searched"]:
            if s["source"] != r["source"]:
                print(f"          also looked: {s['source']:<6} {s['path']} "
                      f"-> {'found' if s['exists'] else 'no'}")
    print("\nartifacts (the route each one is reached through is in --registry / knowledge_routes):")
    for a in reg["artifacts"]:
        tools = ",".join(a["served_by"]["tools"]) or "-"
        print(f"  {'ok     ' if a['present'] else 'MISSING'} {a['id']:<24} layer={str(a['layer']):<5} "
              f"{a['route_kind']:<16} {a['file'] or '(no file)':<40} [{tools}]")
    missing = reg["summary"]["missing_required"]
    if missing:
        print(f"\nmissing required artifacts: {', '.join(missing)} -- the install is broken "
              f"(they ship inside the package); reinstall, or point ESP32S3_DATA_DIR at a good data/ dir.")
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        missing = registry.describe(with_counts=False)["summary"]["missing_required"]
        if missing:                           # refuse to serve nothing: say which install is broken
            print(f"missing required data: {', '.join(missing)}\n"
                  f"data dir in use: {DATA}\n"
                  f"run `esp32s3-hw-mcp --paths` for the search trace.", file=sys.stderr)
            return 2
        build_server().run(transport="stdio")
        return 0
    if "--version" in args:
        print(f"esp32s3-hw-mcp {VERSION}")
        return 0
    if "--help" in args or "-h" in args:
        print(HELP)
        return 0
    if "--list" in args:
        import asyncio

        server = build_server()
        tools = asyncio.run(server.list_tools())
        for t in tools:
            print(f"- {t.name}: {(t.description or '').splitlines()[0]}")
        reg = registry.describe(with_counts=True)
        counts = {a["id"]: a.get("records") for a in reg["artifacts"] if a.get("records") is not None}
        print(f"\n{len(tools)} tools; {len(reg['artifacts'])} registry artifacts "
              f"({reg['summary']['present']} present)")
        print("counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        for role, r in reg["roots"].items():
            print(f"{role:<12} {r['path']}   [{r['source']}]")
        print("corpus: " + ("present (search_manual/get_page enabled)" if pages() is not None
                            else "absent -- build it with --fetch-corpus, or search_manual/get_page will "
                                 "say so"))
        return 0
    if "--paths" in args:
        return print_paths()
    if "--registry" in args:
        print(json.dumps(registry.describe(with_counts=True), ensure_ascii=False, indent=1))
        return 0
    if "--fetch-corpus" in args:
        return cmd_fetch_corpus(args)
    print(f"unknown option(s): {' '.join(args)}\n\n{HELP}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
