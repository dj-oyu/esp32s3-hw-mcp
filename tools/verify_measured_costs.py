#!/usr/bin/env python3
"""Verify the fourth tier's data (data/pie_measured_costs.json) before any of it is served.

This file is hand-curated, not extracted from a PDF: it quotes another project's measurement write-up
(cardputer-adv-pocketjs docs/pie-simd.md). So the gate is built the other way round from verify_pie.py --
there is no extractor to re-run, instead every claim has to check out against two things at once:

  1. the pinned source document: every quote must be the byte-exact slice of the cited lines, the file's
     sha256 must match what was recorded when the lines were read, and every number in the item (both the
     listed ones and any literal inside a statement or caveat) must appear on those same lines. That is what
     stops a number being invented or a paragraph being paraphrased into a slightly different figure;
  2. this repository's own data files: every linked entry (a Table 1.7-2 row, a device-run anchor, a
     semantic finding) must exist, and must hold the value the item says it holds -- so the "esp-dl says
     0-1 cycle" entry cannot quietly stop pointing at the row and the measurement that refute it.

The source document lives in another repository, so on a machine that does not have it (CI checks out one
repository) the quote and number checks report themselves as skipped with the count, and the structural,
provenance, link and search checks still run. Pass --strict to make a missing document a failure.

Usage:
    .venv/bin/python tools/verify_measured_costs.py [--source PATH] [--strict]
Exit code 0 = no failures, 1 = at least one failure (or, with --strict, at least one skip).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data")
DATA_FILE = os.path.join(D, "pie_measured_costs.json")

# Where the sibling project's checkout is expected. The second and third are so that a CI job which checks
# both repositories out side by side can run the full check without being told a path.
SOURCE_CANDIDATES = [
    os.environ.get("ESP32S3_POCKETJS_DOC", ""),
    "/workspace/cardputer-adv-pocketjs/docs/pie-simd.md",
    os.path.join(os.path.dirname(ROOT), "cardputer-adv-pocketjs", "docs", "pie-simd.md"),
]

SECTIONS = ["cost_model", "scaffold_and_formula", "scalar_routines", "pitfalls",
            "measurement_discipline", "disagreements"]

# The vocabulary of provenance kinds. Nothing here may be reported as a document fact, and only
# measured_on_hardware counts as "somebody ran this on silicon" -- and even then, not in this repository:
# see the top-level measured_in_this_repository flag every reply has to carry.
PROVENANCE_KINDS = {
    "measured_on_hardware": "measured on the sibling project's board",
    "derived_from_measurement": "arithmetic over that project's measurements",
    "counted_not_measured": "counted statically (objdump / .s / instruction definitions)",
    "estimate_in_the_source": "marked as an estimate or an assumption by the source itself",
    "document_conflict": "two sources (or one document twice) disagree",
}

# The topics issue #2 asked for. Their absence is a failure, not a warning: the point of the entry is that
# the cost model, the five pitfalls, the scalar prices and the measurement discipline all landed.
REQUIRED = [
    ("cost_model", "pie_instruction_is_one_cycle_whatever_the_kind"),
    ("cost_model", "only_the_128bit_store_costs_extra"),
    ("cost_model", "lower_bound_formula"),
    ("cost_model", "fused_loads_are_free"),
    ("cost_model", "indexed_loads_are_free_but_interleaved_tables_read_slightly_higher"),
    ("cost_model", "real_operation_is_1_3_to_1_4_times_the_floor"),
    ("scaffold_and_formula", "floor_is_per_loop_body_not_per_row"),
    ("scaffold_and_formula", "corrected_formula_adds_the_scaffold_and_the_row_setup"),
    ("scalar_routines", "sqrtf_174_to_188_cycles"),
    ("scalar_routines", "divsf3_55_to_67_cycles"),
    ("scalar_routines", "floorf_and_ceilf_about_78_cycles"),
    ("scalar_routines", "integer_division_about_16_cycles_back_calculated"),
    ("scalar_routines", "divsf3_is_invisible_in_the_disassembly_under_mlongcalls"),
    ("pitfalls", "loopgtz_body_must_fit_in_256_bytes"),
    ("pitfalls", "early_clobber_ampersand_a_is_required_for_a_walking_pointer"),
    ("pitfalls", "pie_is_coprocessor_3_so_it_cannot_be_used_in_an_interrupt_handler"),
    ("pitfalls", "there_is_no_16bit_lane_shift_spell_it_as_a_vmul_with_a_fixed_sar"),
    ("pitfalls", "srcmb_s16_qacc_takes_its_shift_from_an_ar_register"),
    ("measurement_discipline", "the_same_code_reads_15_percent_different_across_builds"),
    ("disagreements", "esp_dl_puts_the_vmul_vrelu_def_stage_at_0_1_cycle"),
    ("disagreements", "integer_division_16_vs_32_within_one_document"),
]

SKIP_SOURCE = "quote/number checks against the source document"

# Numeric literals that are part of a name rather than a quantity: section references, table numbers,
# instruction mnemonics, file names. They are stripped before a statement is scanned for numbers.
STRIP = [
    re.compile(r"§\s*\d+(?:\.\d+)*"),
    re.compile(r"(?:Table|表|section|sectionの)\s*\d+(?:\.\d+)*(?:-\d+)?"),
    re.compile(r"EE\.[A-Za-z0-9._]+"),
    re.compile(r"[A-Za-z0-9_/\.\-]*\.(?:c|h|py|json|s|S)\b"),
    re.compile(r"data/[A-Za-z0-9_\.\-]+"),
]
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def norm(s: str) -> str:
    """Whitespace- and comma-insensitive: the document prints 1,258 where a caller writes 1258."""
    return re.sub(r"[\s,]", "", s)


class Report:
    def __init__(self) -> None:
        self.fail = 0
        self.warn = 0
        self.skip = 0

    def ok(self, msg: str) -> None:
        print(f"  ok    {msg}")

    def bad(self, msg: str) -> None:
        self.fail += 1
        print(f"  FAIL  {msg}")

    def note(self, msg: str) -> None:
        self.warn += 1
        print(f"  warn  {msg}")

    def skipped(self, msg: str, n: int = 1) -> None:
        self.skip += n
        print(f"  skip  {msg}")


def find_source(explicit: str) -> str | None:
    """The pinned document, if this machine has a checkout of the sibling project.

    --source wins, then ESP32S3_POCKETJS_DOC (a CI job sets it to the checkout it fetched, and an explicit
    path that is missing means "run the structural checks only" rather than silently using another copy),
    then the two layouts a checkout is usually found in.
    """
    if explicit:
        return explicit if os.path.exists(explicit) else None
    if SOURCE_CANDIDATES[0]:
        return SOURCE_CANDIDATES[0] if os.path.exists(SOURCE_CANDIDATES[0]) else None
    for cand in SOURCE_CANDIDATES[1:]:
        if cand and os.path.exists(cand):
            return cand
    return None


def dotted(entry, path: str):
    cur = entry
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def check_links(r: Report, item: dict, dry: bool) -> None:
    for link in item.get("linked_entries", []):
        path = os.path.join(ROOT, link["file"])
        if not os.path.exists(path):
            r.bad(f"{item['id']}: linked file {link['file']} is missing")
            continue
        blob = json.load(open(path, encoding="utf-8"))
        loc = link["locate"]
        entries = blob if isinstance(blob, list) else blob.get(loc["list"])
        if entries is None:
            r.bad(f"{item['id']}: {link['file']} has no list {loc['list']!r}")
            continue
        field = loc.get("field") or ""
        hits = []
        for e in entries:
            if not field:
                if isinstance(e, str) and loc["value"] in e:
                    hits.append(e)
            elif isinstance(e, dict) and e.get(field) == loc["value"]:
                hits.append(e)
        if not hits:
            r.bad(f"{item['id']}: {link['file']} :: {loc['list']}[{field or '-'}] == "
                  f"{loc['value']!r} not found")
            continue
        hit = hits[0]
        exp = link.get("expect")
        if exp:
            try:
                got = dotted(hit, exp["path"])
            except Exception as exc:                       # missing key -> a stale link
                r.bad(f"{item['id']}: {link['file']} {loc['value']!r} has no {exp['path']} ({exc})")
                continue
            if "equals" in exp and got != exp["equals"]:
                r.bad(f"{item['id']}: {link['file']} {loc['value']!r} :: {exp['path']} is {got!r}, "
                      f"the entry says {exp['equals']!r}")
                continue
            if "contains" in exp and exp["contains"] not in str(got):
                r.bad(f"{item['id']}: {link['file']} {loc['value']!r} :: {exp['path']} does not contain "
                      f"{exp['contains']!r}: {str(got)[:120]}")
                continue
        if not dry:
            r.ok(f"{item['id']}: link {link['file']} :: {loc['value']} holds"
                 + (f" {exp['path']} == {exp['equals']!r}" if exp and "equals" in exp else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="", help="path to the sibling project's docs/pie-simd.md")
    ap.add_argument("--strict", action="store_true",
                    help="a missing source document (or any warning) is a failure")
    ap.add_argument("--quiet-links", action="store_true", help="report only broken links")
    a = ap.parse_args()
    r = Report()

    if not os.path.exists(DATA_FILE):
        print(f"FAILED: {DATA_FILE} is missing -- nothing to verify")
        return 1
    data = json.load(open(DATA_FILE, encoding="utf-8"))

    print("1. shape")
    items = {}
    for name, meta in [(m["name"], m) for m in data["section_index"]]:
        if name not in SECTIONS:
            r.bad(f"section_index names an unknown section {name!r}")
        if meta["items"] != len(data.get(name, [])):
            r.bad(f"section_index says {name} has {meta['items']} items, the list has "
                  f"{len(data.get(name, []))}")
    if sorted(m["name"] for m in data["section_index"]) != sorted(SECTIONS):
        r.bad(f"section_index does not cover {SECTIONS}")
    else:
        r.ok(f"section_index covers all {len(SECTIONS)} sections with matching counts")
    total = 0
    for section in SECTIONS:
        for item in data[section]:
            total += 1
            if item["id"] in items:
                r.bad(f"duplicate id {item['id']!r}")
            items[item["id"]] = (section, item)
            missing = [k for k in ("id", "statement", "sources", "how_measured", "provenance_kind",
                                   "numbers") if k not in item]
            if missing:
                r.bad(f"{item['id']}: missing field(s) {missing}")
            if not item["statement"].strip().endswith("."):
                r.note(f"{item['id']}: statement does not end in a full stop")
            for src in item.get("sources", []):
                if src["file"] != "docs/pie-simd.md":
                    r.bad(f"{item['id']}: source file is {src['file']!r}, not the pinned document")
                if src["line_start"] > src["line_end"]:
                    r.bad(f"{item['id']}: line range is inverted ({src['line_start']}..{src['line_end']})")
    r.ok(f"{total} items across {len(SECTIONS)} sections, {len(items)} unique ids")

    print("2. provenance")
    prov = data["provenance"]
    if prov.get("kind") != "measured_on_hardware" or prov.get("measured_in_this_repository") is not False:
        r.bad("top-level provenance must say measured_on_hardware and measured_in_this_repository: false")
    else:
        r.ok("top-level provenance: measured on the sibling project's hardware, not in this repository")
    bad_kind = [(i["id"], i["provenance_kind"]) for _, i in items.values()
                if i["provenance_kind"] not in PROVENANCE_KINDS]
    if bad_kind:
        r.bad(f"items with a provenance kind outside the vocabulary: {bad_kind}")
    else:
        counts = {}
        for _, i in items.values():
            counts[i["provenance_kind"]] = counts.get(i["provenance_kind"], 0) + 1
        r.ok(f"all items carry one of the {len(PROVENANCE_KINDS)} provenance kinds: {counts}")
    for _, i in items.values():
        est, kind = bool(i.get("estimate")), i["provenance_kind"]
        if est and kind != "estimate_in_the_source":
            r.bad(f"{i['id']}: estimate=true with provenance_kind={kind!r}")
        if kind == "estimate_in_the_source" and not est:
            r.bad(f"{i['id']}: provenance_kind=estimate_in_the_source without estimate=true")
        if est and not i.get("estimate_marker"):
            r.bad(f"{i['id']}: estimate without the marker word the source uses")
    if not any(i.get("estimate") for _, i in items.values()):
        r.bad("no item is marked as an estimate, although the source has some")

    print("3. quotes and numbers against the pinned document")
    section3_fail_before = r.fail
    source = find_source(a.source)
    if source is None:
        tried = [c for c in SOURCE_CANDIDATES[1:] if c]
        r.skipped(f"{SKIP_SOURCE} (no checkout of the sibling project found; tried "
                  f"{SOURCE_CANDIDATES[0] or '(no ESP32S3_POCKETJS_DOC)'} and {tried})", 1)
        doc_lines, recorded_sha = None, data["source"]["sha256"]
    else:
        raw = open(source, encoding="utf-8").read()
        doc_lines = raw.splitlines()
        recorded_sha = data["source"]["sha256"]
        actual = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if actual != recorded_sha:
            r.bad(f"the source document's sha256 is {actual[:16]}, the data records {recorded_sha[:16]}: "
                  f"the document moved, so every quote must be re-read before this file is served")
        else:
            r.ok(f"source document sha256 matches the recorded {recorded_sha[:16]} "
                 f"({len(doc_lines)} lines, data says {data['source']['lines_total']})")
        if len(doc_lines) != data["source"]["lines_total"]:
            r.bad(f"source has {len(doc_lines)} lines, the data records {data['source']['lines_total']}")

    for _, item in items.values():
        union = ""
        skipped_ranges = False
        for src in item["sources"]:
            if doc_lines is None:
                continue
            if not 1 <= src["line_start"] <= src["line_end"] <= data["source"]["lines_total"]:
                r.bad(f"{item['id']}: line range {src['line_start']}..{src['line_end']} is outside the "
                      f"document")
                skipped_ranges = True
                continue
            want = "\n".join(doc_lines[src["line_start"] - 1:src["line_end"]])
            if src["quote"] != want:
                r.bad(f"{item['id']}: quote is not the verbatim slice of lines "
                      f"{src['line_start']}..{src['line_end']}")
                skipped_ranges = True
                continue
            union += want + "\n"
        if doc_lines is None or skipped_ranges:
            continue
        cited = norm(union)
        if item.get("estimate") and norm(item["estimate_marker"]) not in cited:
            r.bad(f"{item['id']}: estimate_marker {item['estimate_marker']!r} is not on the cited lines")
        # A number may live on any of the item's cited lines; it must live on one of them.
        ranges = ", ".join("%d..%d" % (s["line_start"], s["line_end"]) for s in item["sources"])
        for num in item["numbers"]:
            if norm(num["as_printed"]) not in cited:
                r.bad(f"{item['id']}: number {num['as_printed']!r} ({num['what']}) is not on the cited "
                      f"lines {ranges}")
        # Every literal in the prose must come from the cited lines as well: this is the check that stops a
        # plausible-looking figure being written in rather than quoted.
        prose = " ".join([item["statement"]] + list(item.get("caveats", [])))
        for pat in STRIP:
            prose = pat.sub(" ", prose)
        stray = sorted({m.group(0) for m in NUM.finditer(prose) if norm(m.group(0)) not in cited})
        if stray:
            r.bad(f"{item['id']}: number(s) in the statement/caveats that are not on the cited lines: "
                  f"{stray}")
        # Whether or not the document is here, a listed value must match the literal it was printed as.
        for num in item["numbers"]:
            lit = norm(num["as_printed"])
            val = num.get("value")
            if val is None:
                if re.fullmatch(r"\d+\.\d+", lit):
                    r.bad(f"{item['id']}: {num['as_printed']!r} looks numeric but carries no value")
                continue
            try:
                parsed = float(lit)
            except ValueError:
                r.bad(f"{item['id']}: {num['as_printed']!r} does not parse as a number, so it must not "
                      f"carry value={val!r}")
                continue
            if abs(parsed - float(val)) > 1e-9:
                r.bad(f"{item['id']}: {num['as_printed']!r} is recorded as value={val!r}")
    if doc_lines is not None:
        if r.fail == section3_fail_before:
            r.ok(f"every quote is the verbatim slice of its cited lines and every listed number appears "
                 f"there ({total} items)")
        else:
            print(f"  ....  {r.fail - section3_fail_before} failure(s) above in this section")

    print("4. links into this repository's own data")
    for _, item in items.values():
        check_links(r, item, a.quiet_links)

    print("5. recorded searches re-run")
    for _, item in items.values():
        chk = item.get("issue_claim_check")
        if not chk:
            continue
        blob = open(os.path.join(ROOT, chk["search"]["file"]), encoding="utf-8").read()
        found = sum(blob.count(p) for p in chk["search"]["patterns"])
        if found != chk["search"]["matches"]:
            r.bad(f"{item['id']}: the recorded search of {chk['search']['file']} counted "
                  f"{chk['search']['matches']} match(es), now {found}; the claim it says is absent may "
                  f"have been added")
        else:
            r.ok(f"{item['id']}: {chk['search']['file']} still has {found} match(es) for "
                 f"{chk['search']['patterns']}, as recorded")

    print("6. coverage of the topics issue #2 asked for")
    missing = [(s, i) for s, i in REQUIRED if i not in items]
    if missing:
        r.bad(f"missing items: {missing}")
    else:
        r.ok(f"all {len(REQUIRED)} required topics are present")
    wrong_section = [(s, i) for s, i in REQUIRED if i in items and items[i][0] != s]
    if wrong_section:
        r.bad(f"items in the wrong section: {wrong_section}")

    print(f"\n{'FAILED' if r.fail else 'PASSED'}: {r.fail} failure(s), {r.warn} warning(s), "
          f"{r.skip} skipped")
    if a.strict and (r.warn or r.skip):
        return 1
    return 1 if r.fail else 0


if __name__ == "__main__":
    sys.exit(main())
