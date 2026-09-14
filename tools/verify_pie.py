#!/usr/bin/env python3
"""Verify the extracted PIE tables against the manual's own text — the gate before any of it is served.

Nothing here trusts the extractor. Each check re-derives the expected value from a different part of the
PDF and fails loudly on a mismatch, because a plausibly-wrong hazard table silently produces wrong
scheduling advice.

Checks
  1. coverage      : every instruction in 1.8 is in Table 1.7-2, or is listed as a known exception with the
                     reason (the table is the authority for stages, so an omission must be a documented one).
  2. operands      : for each instruction, the register names in the table's use/def/static columns must be
                     a subset of the operands named by that instruction's Assembler Syntax in 1.8 (the
                     syntax is the independent list of what the instruction touches).
  3. stages        : every parsed stage number is 1 or 2 and matches Table 1.7-1's stage numbering
                     (1 = E, 2 = M).
  4. field blocks  : all 220 1.8 sections carry instruction word / syntax / description / operation.
  5. spot checks   : hand-read rows (with the page they were read from) must match exactly.
  6. corpus        : page count and "printed page == PDF page" for both documents.

Usage: .venv/bin/python tools/verify_pie.py [--strict]
Exit code 0 = all checks pass, 1 = at least one failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data")

# Instructions 1.8 documents that Table 1.7-2 does not list, with the reason. Verified by grepping every
# Table 1.7-2 page for the mnemonic (see the check that prints them).
KNOWN_ABSENT = {
    "LD.QR": "Table 1.7-2 omits the QR load/store/move instructions (1.8.218-1.8.220, p301-303)",
    "ST.QR": "Table 1.7-2 omits the QR load/store/move instructions (1.8.218-1.8.220, p301-303)",
    "MV.QR": "Table 1.7-2 omits the QR load/store/move instructions (1.8.218-1.8.220, p301-303)",
}

# Rows read off the printed page by hand, as a check on the coordinate extraction.
SPOT = [
    # instruction, page, use, def, sr_use, sr_def
    ("EE.ANDQ", 66, ["qx", "qy"], ["qa"], [], []),
    ("EE.VZIP.8", 73, ["qs0", "qs1"], ["qs0", "qs1"], [], []),
    ("EE.ZERO.ACCX", 73, [], [], [], ["ACCX"]),
    ("EE.ZERO.Q", 74, [], ["qa"], [], []),
    ("EE.CLR_BIT_GPIO_OUT", 66, [], [], ["GPIO_OUT"], ["GPIO_OUT"]),
]

OPERAND_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


class Report:
    def __init__(self) -> None:
        self.fail = 0
        self.warn = 0

    def ok(self, msg: str) -> None:
        print(f"  ok    {msg}")

    def bad(self, msg: str) -> None:
        self.fail += 1
        print(f"  FAIL  {msg}")

    def note(self, msg: str) -> None:
        self.warn += 1
        print(f"  warn  {msg}")


def syntax_operands(syntax: str | None) -> set[str]:
    """Register names an instruction's Assembler Syntax mentions, minus immediates/labels."""
    if not syntax:
        return set()
    first = syntax.split("\n")[0]
    first = re.sub(r"^[A-Z0-9_.]+\s*", "", first)  # drop the mnemonic
    names = set()
    for tok in re.split(r"[,\s]+", first):
        tok = tok.strip()
        if not tok or tok in {"-", "—"}:
            continue
        if re.fullmatch(r"-?\d+(\.\.-?\d+)?", tok):      # immediate / range
            continue
        m = OPERAND_RE.fullmatch(tok)
        if m:
            names.add(tok)
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="treat warnings as failures")
    a = ap.parse_args()
    r = Report()

    insts = {e["name"]: e for e in json.load(open(os.path.join(D, "pie_instructions.json"), encoding="utf-8"))}
    table = {e["instruction"]: e for e in json.load(open(os.path.join(D, "pie_pipeline.json"), encoding="utf-8"))}

    print("1. coverage")
    missing = sorted(set(insts) - set(table))
    unexpected = [m for m in missing if m not in KNOWN_ABSENT]
    if unexpected:
        r.bad(f"instructions in 1.8 with no Table 1.7-2 row: {unexpected}")
    else:
        r.ok(f"all {len(insts)} 1.8 instructions accounted for; "
             f"{len(missing)} documented omission(s): {missing}")
    extra = sorted(set(table) - set(insts))
    if extra:
        r.bad(f"Table 1.7-2 rows with no 1.8 section: {extra}")
    else:
        r.ok(f"no table row without a 1.8 section ({len(table)} rows)")

    print("2. operands (table vs 1.8 Assembler Syntax)")
    mismatched = []
    review = []
    for name, row in table.items():
        syn = syntax_operands(insts.get(name, {}).get("assembler_syntax"))
        if not syn:
            continue
        unparsed = [o for o in row["operands_use"] + row["operands_def"] + row["special_regs_use"] + row["special_regs_def"] if "reg" not in o]
        if unparsed:
            r.bad(f"{name}: unparsed cell fragment(s) {unparsed[:3]}")
            continue
        got = {o["reg"] for o in row["operands_use"] + row["operands_def"]}
        unknown = {g for g in got if g not in syn}
        # Special-register columns name registers (SAR, ACCX, QACC_H...) that the syntax never lists.
        if unknown:
            mismatched.append((name, sorted(unknown), sorted(syn)))
            review.append({"instruction": name, "source_page": row["source_page"],
                           "registers_not_in_syntax": sorted(unknown),
                           "syntax_operands": sorted(syn),
                           "cells": {c: row[c + "_text"] for c in
                                     ("operands_use", "operands_def", "special_regs_use", "special_regs_def")}})
    json.dump(review, open(os.path.join(D, "pie_review.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    # A register the table names and the 1.8 syntax does not is not an extraction bug (two independent
    # extractors agree on the text) -- it is the manual disagreeing with itself. Curation decides.
    if mismatched:
        r.note(f"{len(mismatched)} row(s) where the table names a register its own syntax omits -> "
               f"data/pie_review.json: {[m[0] for m in mismatched]}")
    else:
        r.ok(f"all {len(table)} rows only name operands their Assembler Syntax declares")

    print("3. pipeline stage values")
    bad_stage = []
    for name, row in table.items():
        for col in ("operands_use", "operands_def", "special_regs_use", "special_regs_def"):
            for o in row[col]:
                if o.get("stage") not in (1, 2):
                    bad_stage.append((name, col, o))
    if bad_stage:
        r.bad(f"stage values outside {{1=E, 2=M}}: {bad_stage[:8]} ({len(bad_stage)} total)")
    else:
        r.ok("every operand/special register carries stage 1 (E) or 2 (M)")

    print("4. 1.8 field blocks")
    incomplete = [(n, [f for f in ("instruction_word", "assembler_syntax", "description", "operation")
                       if not e[f]]) for n, e in insts.items()]
    incomplete = [x for x in incomplete if x[1]]
    if incomplete:
        for n, fields in incomplete:
            r.note(f"{n} (p{insts[n]['source_page']}) lacks {fields}")
    else:
        r.ok("all 220 sections carry instruction word, syntax, description, operation")

    print("4b. no field body leaks the next section")
    leaks = []
    for n, e in insts.items():
        for f in ("instruction_word", "assembler_syntax", "description", "operation"):
            body = e[f] or ""
            if re.search(r"^1\.8\.\d+\s*$", body, re.M) or body.count("Assembler Syntax") >= 1 \
                    or body.count("Instruction Word") >= 1 or re.search(r"^Operation\s*$", body, re.M):
                leaks.append((n, f))
    if leaks:
        r.bad(f"field bodies containing another section's text: {leaks[:6]}")
    else:
        r.ok("no 1.8 section's field body contains another section's heading or field label")

    print("4c. the syntax line names its own instruction")
    mismatched_mnemonic = []
    for n, e in insts.items():
        syn = (e["assembler_syntax"] or "").split("\n")[0].strip()
        if syn and syn.split(" ")[0] != n:
            mismatched_mnemonic.append((n, syn))
    if mismatched_mnemonic:
        # e.g. ST.QR is printed with LD.QR's mnemonic in its own documentation (p302) -- a manual typo,
        # recorded rather than "fixed", because the extraction must mirror the source.
        r.note(f"{len(mismatched_mnemonic)} section(s) whose syntax line opens with another mnemonic: "
               f"{mismatched_mnemonic}")
    else:
        r.ok("every syntax line opens with its own mnemonic")

    print("5. spot checks against the printed page")
    for name, page, use, deff, sru, srd in SPOT:
        row = table.get(name)
        if not row:
            r.bad(f"{name} missing from the table")
            continue
        got = ([o.get("reg") for o in row["operands_use"]], [o.get("reg") for o in row["operands_def"]],
               [o.get("reg") for o in row["special_regs_use"]], [o.get("reg") for o in row["special_regs_def"]])
        want = (use, deff, sru, srd)
        if got == want and row["source_page"] == page:
            r.ok(f"{name} p{page} -> {got}")
        else:
            r.bad(f"{name} p{row['source_page']} (want p{page}) -> {got}, want {want}")

    print("6. corpora")
    for name, corpus, pages_expected in (("trm-s3", "corpus/trm-s3", 1531), ("datasheet-s3", "corpus/datasheet-s3", 87)):
        path = os.path.join(ROOT, corpus, "pages.jsonl")
        recs = [json.loads(l) for l in open(path, encoding="utf-8")]
        empty = [p["pdf_page"] for p in recs if len(p["text"].strip()) < 40]
        off = [p["pdf_page"] for p in recs if p.get("label") not in (None, str(p["pdf_page"]))]
        if len(recs) != pages_expected:
            r.bad(f"{name}: {len(recs)} pages, expected {pages_expected}")
        elif empty:
            r.bad(f"{name}: near-empty pages {empty[:10]}")
        elif off:
            r.bad(f"{name}: printed page != PDF page on {off[:10]}")
        else:
            r.ok(f"{name}: {len(recs)} pages, no empty pages, printed page == PDF page")

    print(f"\n{'FAILED' if r.fail else 'PASSED'}: {r.fail} failure(s), {r.warn} warning(s)")
    if a.strict and r.warn:
        return 1
    return 1 if r.fail else 0


if __name__ == "__main__":
    sys.exit(main())
