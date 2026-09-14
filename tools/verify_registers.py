#!/usr/bin/env python3
"""Verify data/registers.json and data/peripheral_map.json against the manual.

The extractor is never trusted: every check re-derives its expectation from a *different* view of the PDF
(pypdf's text layer, the outline, or a hand-read page), so an extraction bug cannot validate itself. A
register index that silently loses or mis-columns rows is worse than none, because every answer built on it
looks authoritative.

Checks
  1. coverage      : register names the PDF spells on a Register Summary page must be in the output, compared
                     with pypdf (the extractor uses PyMuPDF) and with non-alphanumerics stripped, so a cell
                     that loses an underscore still matches.
  2. shape         : names match the manual's naming, offsets parse, access tokens are documented ones, and
                     the absolute/relative address flag agrees with the token shape.
  3. collisions    : within one section and one register family, no two registers share an offset.
  4. sections      : every row is attributed to a section that exists in the outline.
  5. spot checks   : rows read off the printed page by hand, compared exactly.
  6. peripheral map: boundaries ascend, ranges do not overlap, size matches the range.

Usage: .venv/bin/python tools/verify_registers.py [--strict]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data")
sys.path.insert(0, os.path.join(ROOT, "tools"))

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*_REG$")
NAME_PARAM_RE = re.compile(r"^[A-Z][A-Z0-9_]*_REG\s*\(n:\s*[\d\-]+\)$")
HEX_RE = re.compile(r"^0x[0-9A-Fa-f]{2,8}$")
HEX_UNDERSCORE_RE = re.compile(r"^0x[0-9A-Fa-f]{4}_[0-9A-Fa-f]{4}$")
ACCESS_TOKENS = {"R/W", "RO", "WO", "varies", "R/W1C", "R/W1S", "R/W1T", "R/W1C/SS", "R/WTC/SS",
                 "R/W1S/SS", "R/W1T/SS", "RW"}
CANDIDATE_RE = re.compile(r"\b[A-Z][A-Z0-9_]*(?: [A-Z0-9_]+)*_REG\b")

SPOT = [
    # name, page, offset, access
    ("EFUSE_PGM_DATA0_REG", 423, "0x0000", "R/W"),
    ("GDMA_IN_CONF0_CH0_REG", 369, "0x0000", "R/W"),
    ("GDMA_OUT_CONF1_CH3_REG", 370, "0x02A4", "R/W"),
    ("RTC_CNTL_ULP_CP_TIMER_REG", 332, "0x00FC", "varies"),
    ("RTC_CNTL_ULP_CP_TIMER_1_REG", 332, "0x0134", "R/W"),
    ("TWAI_MODE_REG", 1213, "0x0000", "R/W"),
    ("INTERRUPT_CORE0_MAC_INTR_MAP_REG", 547, "0x0000", "R/W"),      # rotated (landscape) table
    ("INTERRUPT_CORE1_MAC_INTR_MAP_REG", 550, "0x0800", "R/W"),
    ("IO_MUX_GPIO0_REG", 498, "0x0004", "R/W"),
    ("RNG_DATA_REG", 921, "0x6003_507C", "RO"),                       # absolute address, not an offset
]


def canon(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


class Report:
    def __init__(self) -> None:
        self.fail = 0
        self.warn = 0

    def ok(self, m): print(f"  ok    {m}")
    def bad(self, m): self.fail += 1; print(f"  FAIL  {m}")
    def note(self, m): self.warn += 1; print(f"  warn  {m}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    r = Report()

    import pypdf
    import extract_registers as X

    rows = json.load(open(os.path.join(D, "registers.json"), encoding="utf-8"))
    pmap = json.load(open(os.path.join(D, "peripheral_map.json"), encoding="utf-8"))
    have = {canon(x["name"]) for x in rows}
    secs = X.summary_sections(os.path.join(ROOT, "corpus/trm-s3"))

    print("1. coverage (pypdf text layer vs extraction, per Register Summary page range)")
    reader = pypdf.PdfReader(os.path.join(ROOT, "sources/esp32-s3_technical_reference_manual_en.pdf"))
    missing: list[tuple[str, int]] = []
    checked = set()
    for s in secs:
        if (s["first"], s["last"]) in checked:
            continue
        checked.add((s["first"], s["last"]))
        for pno in range(s["first"], s["last"] + 1):
            text = reader.pages[pno - 1].extract_text() or ""
            for m in CANDIDATE_RE.finditer(text):
                cand = m.group(0)
                # Skip fragments and prose mentions, which are not registers the extraction missed:
                #  * "SIZE_INTR_MAP_REG" continues a name broken by the cell width ("…_VIOLATE_"),
                #  * "W_CONSTRAIN_0_REG" is the tail of "…_SPLIT_LINE_W_CONSTRAIN_0_REG" re-ordered by pypdf,
                #  * "I2S_LC_HUNG_CONF_REG," is mentioned in a sentence.
                if m.start() > 0 and text[m.start() - 1] not in " \t\n":
                    continue
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.start())
                line = text[line_start:line_end if line_end != -1 else len(text)]
                if not re.search(r"0x[0-9A-Fa-f]{2,8}", line):     # a register-summary row prints its address
                    continue
                if canon(cand) not in have:
                    missing.append((cand, pno))
    if missing:
        r.bad(f"{len(missing)} name(s) spelled in the PDF but absent from registers.json, "
              f"e.g. {missing[:8]}")
    else:
        r.ok(f"every register name on the {len(checked)} summary ranges is present "
             f"({len({x['name'] for x in rows})} distinct names)")

    print("2. shape")
    bad_names = sorted({x["name"] for x in rows
                        if not (NAME_RE.match(x["name"]) or NAME_PARAM_RE.match(x["name"]))})
    bad_acc = sorted({x["access"] for x in rows} - ACCESS_TOKENS - {None})
    bad_off = [x["name"] for x in rows
               if not (HEX_RE.match(x["offset"] or "") or HEX_UNDERSCORE_RE.match(x["offset"] or "")
                       or x["offset_expr"])]
    bad_flag = [x["name"] for x in rows
                if x["address_is_absolute"] != bool(HEX_UNDERSCORE_RE.match(x["offset"] or ""))]
    bad_param = [x["name"] for x in rows
                 if x["offset_is_parameterised"] != (bool(x["offset_expr"]) or bool(NAME_PARAM_RE.match(x["name"])))]
    if bad_names:
        r.bad(f"names that are not register names: {bad_names[:8]}")
    else:
        r.ok(f"all {len(rows)} names match the manual's naming (parameterised ones included)")
    if bad_off:
        r.bad(f"{len(bad_off)} row(s) without a usable offset: {bad_off[:6]}")
    else:
        r.ok("every row carries an offset or an offset expression")
    if bad_acc:
        r.bad(f"access tokens outside the documented set: {bad_acc}")
    else:
        r.ok("every access token is one of the manual's documented access types")
    if bad_flag:
        r.bad(f"absolute-address flag disagrees with the token: {bad_flag[:6]}")
    else:
        r.ok(f"{sum(1 for x in rows if x['address_is_absolute'])} row(s) marked as absolute addresses")
    if bad_param:
        r.bad(f"parameterised flag disagrees: {bad_param[:6]}")
    else:
        r.ok(f"{sum(1 for x in rows if x['offset_is_parameterised'])} parameterised row(s) flagged")

    print("3. offsets unique within a section and register family")
    coll = []
    for sec in {x["section"] for x in rows}:
        seen: dict[tuple[str, str], str] = {}
        for x in rows:
            if x["section"] != sec or not x["offset"]:
                continue
            family = x["name"].split("_")[0]
            key = (family, x["offset"])
            if key in seen:
                coll.append((sec, family, x["offset"], seen[key], x["name"]))
            seen[key] = x["name"]
    if coll:
        r.bad(f"{len(coll)} offset collision(s) inside one section+family: {coll[:5]}")
    else:
        r.ok(f"no two registers of one family share an offset in the same section "
             f"({len({x['section'] for x in rows})} sections)")

    print("4. section attribution")
    titles = {" ".join(x["title"].split())
              for x in json.load(open(os.path.join(ROOT, "corpus/trm-s3/outline.json"), encoding="utf-8"))}
    unknown = sorted({x["section"] for x in rows if x["section"] not in titles})
    if unknown:
        r.note(f"section labels not found verbatim in the outline: {unknown[:6]}")
    else:
        r.ok("every row's section is an outline 'Register Summary' entry")

    print("5. spot checks against the printed page")
    for name, page, off, acc in SPOT:
        hits = [x for x in rows if x["name"] == name]
        h = next((x for x in hits if x["source_page"] == page), None)
        if h is None:
            r.bad(f"{name} p{page}: not found (pages: {[x['source_page'] for x in hits]})")
        elif h["offset"] != off or h["access"] != acc:
            r.bad(f"{name} p{page}: got {h['offset']} {h['access']}, want {off} {acc}")
        else:
            r.ok(f"{name} p{page} -> {h['offset']} {h['access']}")

    print("6. peripheral map (Table 4.3-3)")
    bad = []
    for i, e in enumerate(pmap):
        lo = int(e["low_address"].replace("_", ""), 16)
        hi = int(e["high_address"].replace("_", ""), 16) if e["high_address"] else None
        if hi is None or hi <= lo:
            bad.append((e["target"], e["low_address"], e["high_address"]))
        elif e["size_kb"] is not None and e["size_kb"] != round((hi - lo + 1) / 1024):
            bad.append((e["target"], "size", e["size_kb"], round((hi - lo + 1) / 1024)))
        elif i and int(pmap[i - 1]["high_address"].replace("_", ""), 16) >= lo:
            bad.append((e["target"], "overlaps previous", e["low_address"]))
    if bad:
        r.bad(f"{len(bad)} inconsistent peripheral row(s): {bad[:5]}")
    else:
        r.ok(f"{len(pmap)} rows: boundaries ascend, no overlaps, size == range")

    print(f"\n{'FAILED' if r.fail else 'PASSED'}: {r.fail} failure(s), {r.warn} warning(s)")
    if a.strict and r.warn:
        return 1
    return 1 if r.fail else 0


if __name__ == "__main__":
    sys.exit(main())
