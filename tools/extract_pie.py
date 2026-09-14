#!/usr/bin/env python3
"""Extract machine-readable PIE (Processor Instruction Extensions) data from the ESP32-S3 TRM PDF.

Three outputs, one per primary-source artifact:

  data/pie_instructions.json  <- TRM 1.8 (printed pages 76-303), one entry per extended instruction:
                                 encoding bit pattern, assembler syntax, description, operation pseudocode,
                                 + the printed page each field came from.
  data/pie_pipeline.json      <- TRM Table 1.7-2 (printed pages 66-73): per-instruction operand and special
                                 register use/def pipeline stages. This is the hazard table the scheduling
                                 tools consume. Extracted by PDF coordinates, not by text order, because the
                                 table's columns collapse into an unusable stream in plain extraction.
  data/pie_hazards.md         <- TRM 1.7.1-1.7.3 prose (printed pages 65-75) with page markers, so every
                                 scheduling rule can be quoted back to its page.

Source of truth: sources/esp32-s3_technical_reference_manual_en.pdf (TRM v1.8), printed page == PDF page.

Usage:
    .venv/bin/python tools/extract_pie.py [--pdf PATH] [--out DIR] [--diag]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# Printed pages (== PDF pages for this manual).
SEC_FIRST, SEC_LAST = 76, 303      # 1.8 instruction functional description
TBL_FIRST, TBL_LAST = 66, 74       # Table 1.7-2 (runs to the top of p74)
HAZARD_FIRST, HAZARD_LAST = 65, 75  # 1.7 instruction performance

# Column x positions in Table 1.7-2 (points, from the PDF text matrix).
COLS = [("mnemonic", 100.0), ("operand_use", 240.0), ("operand_def", 310.0),
        ("special_use", 400.0), ("special_def", 1e9)]


def cell_of(x: float) -> str:
    for name, hi in COLS:
        if x < hi:
            return name
    return COLS[-1][0]


def items_of(page) -> list[tuple[float, float, str]]:
    """(x, y, text) for every text fragment on the page, in top-to-bottom order."""
    out: list[tuple[float, float, str]] = []

    def visitor(text, cm, tm, font_dict, font_size):
        t = text.strip()
        if t:
            out.append((round(tm[4], 1), round(tm[5], 1), t))

    page.extract_text(visitor_text=visitor)
    return out


INSTRUCTION_RE = re.compile(r"^(EE\.[A-Z0-9_.]+|(?:LD|ST|MV)\.QR)$")

# Running heads/footers that repeat on every page of the manual.
FOOTER = re.compile(r"^(Espressif Systems \d+|Submit Documentation Feedback|"
                    r"ESP32-S3 TRM \(Version [\d.]+\)|"
                    r"Chapter 1 Processor Instruction Extensions \(PIE\) GoBack)$")


STAGE_PAIR = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*([12])(?=$|[,\s])")


def parse_stage_cell(cell: str, kind: str) -> list[dict]:
    """'qx 1, qy 1' -> [{'reg': 'qx', 'stage': 1}]; '—' -> []; unresolvable text -> [{'raw': ...}].

    The manual's text layer drops the space between a subscripted operand and its stage number, so cells
    arrive as 'qv2,as01,as1,' for "qv 2, as0 1, as 1". The register name is therefore matched greedily and
    the trailing digit is the pipeline stage; whatever is left over is reported as `raw` so a human sees it
    instead of a silently truncated operand list.
    """
    cell = cell.replace("\n", " ").replace("—", " ").strip()
    if not cell:
        return []
    out: list[dict] = []
    leftover = STAGE_PAIR.sub(lambda m: (out.append({"reg": m.group(1), "stage": int(m.group(2))}), "")[1], cell)
    residue = re.sub(r"[\s,]+", " ", leftover).strip(" ,")
    if residue:
        out.append({"raw": residue})
    return out


def extract_table(reader, diag: bool = False) -> list[dict]:
    """Table 1.7-2 -> one row per instruction, columns recovered by x-coordinate.

    The table's own header repeats on every page, and a row whose cells wrap onto the next page has no
    instruction cell on that page: those fragments are carried into the previous page's last row.
    """
    rows: list[dict] = []

    for pno in range(TBL_FIRST, TBL_LAST + 1):
        page = reader.pages[pno - 1]
        items = items_of(page)
        body = [(x, y, t) for x, y, t in items
                if -700 < y < -1 and not FOOTER.match(t)]
        # A page that carries the next section heading (1.7.2 / 1.7.3 or a later chapter) has the table only
        # above that heading: everything below it is prose that would otherwise attach to the last row.
        heads = [y for x, y, t in body if re.match(r"^1\.7\.[2-9]$", t) or re.match(r"^[2-9]\.\d+(\.\d+)?$", t)]
        if heads:
            body = [(x, y, t) for x, y, t in body if y > max(heads)]
        # Drop the repeated table header ("Instruction | Use | Def | ..."): every fragment at or above it is
        # either the header itself or running prose on the table's first page.
        head_y = [y for x, y, t in body if cell_of(x) == "mnemonic" and t == "Instruction"]
        if head_y:
            cutoff = min(head_y) - 10.0
            body = [(x, y, t) for x, y, t in body if y < cutoff]

        per_col: dict[str, list[tuple[float, float, str]]] = {c[0]: [] for c in COLS}
        for x, y, t in body:
            per_col[cell_of(x)].append((y, x, t))

        anchors = sorted(per_col["mnemonic"], key=lambda z: -z[0])
        page_rows = []
        for y, _x, t in anchors:
            if not INSTRUCTION_RE.match(t):
                if diag:
                    print(f"  [diag] p{pno}: unanchored text in instruction column: {t!r}", file=sys.stderr)
                if page_rows:
                    page_rows[-1]["instruction"] += " " + t
                continue
            page_rows.append({"instruction": t, "source_page": pno,
                              "operand_use": [], "operand_def": [],
                              "special_use": [], "special_def": [], "_ay": y})

        anchor_ys = [r["_ay"] for r in page_rows]  # descending y: [topmost, ..., bottom]
        bounds = [(anchor_ys[i] + anchor_ys[i + 1]) / 2 for i in range(len(anchor_ys) - 1)]
        top_edge = anchor_ys[0] + 8.0 if anchor_ys else 0.0

        def owner_of(y: float):
            """Row a fragment belongs to. Cells are vertically centred on their row's anchor, so a wrapped
            line sits at most one line-height (7.8pt) off it: band the fragments by the midpoints between
            anchors. Above <topmost anchor + 8pt> no row of this page can own it -- see the page-break case."""
            if y > top_edge:
                return None
            for i, b in enumerate(bounds):
                if y >= b:
                    return anchor_ys[i]
            return anchor_ys[-1]

        for col in ("operand_use", "operand_def", "special_use", "special_def"):
            for y, x, t in per_col[col]:
                owner = owner_of(y)
                if owner is None:
                    # A row whose anchor is on the previous page and whose cell wraps onto this one.
                    if rows:
                        rows[-1][col].append((y, x, t))
                    elif diag:
                        print(f"  [diag] p{pno}: orphan fragment above the table: {t!r}", file=sys.stderr)
                    continue
                next(r for r in page_rows if r["_ay"] == owner)[col].append((y, x, t))

        rows.extend(page_rows)

    out: list[dict] = []
    for r in rows:
        row = {"instruction": r["instruction"], "source_page": r["source_page"]}
        for col, key in (("operand_use", "operands_use"), ("operand_def", "operands_def"),
                         ("special_use", "special_regs_use"), ("special_def", "special_regs_def")):
            # Visual order: a cell that wrapped shows up as fragments emitted out of order by the PDF.
            text = " ".join(t for _y, _x, t in sorted(r[col], key=lambda f: (-f[0], f[1]))).strip()
            row[key] = parse_stage_cell(text, col)
            row[key + "_text"] = text
        out.append(row)
    return out


def sections_from_outline(corpus_dir: str, first: int, last: int) -> list[tuple[int, str]]:
    outl = json.load(open(os.path.join(corpus_dir, "outline.json"), encoding="utf-8"))
    secs = [(e["pdf_page"], e["title"]) for e in outl
            if re.match(r"^1\.8\.\d+ ", e["title"])]
    outl2 = []
    for i, (p, t) in enumerate(secs):
        if not (first <= p <= last):
            continue
        name = re.sub(r"^1\.8\.\d+\s+", "", t).strip()
        outl2.append((p, name))
    return outl2


FIELD_RE = {
    "instruction_word": re.compile(r"^Instruction Word\s*$", re.M),
    "assembler_syntax": re.compile(r"^Assembler Syntax\s*$", re.M),
    "description": re.compile(r"^Description\s*$", re.M),
    "operation": re.compile(r"^Operation\s*$", re.M),
}
NOISE = re.compile(r"^(Chapter 1 Processor Instruction Extensions \(PIE\) GoBack|"
                   r"Espressif Systems \d+|Submit Documentation Feedback|"
                   r"ESP32-S3 TRM \(Version 1\.8\))$")


def extract_instructions(pages: list[str], secs: list[tuple[int, str]], diag: bool = False) -> list[dict]:
    out = []
    for idx, (pno, name) in enumerate(secs):
        end = secs[idx + 1][0] if idx + 1 < len(secs) else SEC_LAST
        chunks = []
        for p in range(pno, end + 1):
            for line in pages[p - 1].split("\n"):
                s = line.rstrip()
                if NOISE.match(s.strip()):
                    continue
                chunks.append((p, s))
        # drop the section heading line itself
        chunks = [(p, s) for p, s in chunks if not re.match(rf"^1\.8\.\d+\s+{re.escape(name)}\s*$", s.strip())]

        heads = []  # (field, index in chunks)
        for i, (p, s) in enumerate(chunks):
            for field, rx in FIELD_RE.items():
                if rx.match(s.strip()) and not any(h[0] == field for h in heads):
                    heads.append((field, i))
        heads.sort(key=lambda h: h[1])
        entry = {"name": name, "source_page": pno, "sections_page_range": [pno, end],
                 "instruction_word": None, "assembler_syntax": None, "description": None,
                 "operation": None, "raw_pages": [pno, end]}
        pages_of: dict[str, int] = {}
        for j, (field, i) in enumerate(heads):
            stop = heads[j + 1][1] if j + 1 < len(heads) else len(chunks)
            body_lines = []
            for p, s in chunks[i + 1:stop]:
                body_lines.append(s)
                pages_of.setdefault(field, p)
            entry[field] = "\n".join(body_lines).strip("\n")
            entry[field + "_page"] = pages_of.get(field)
        for f in FIELD_RE:
            entry.setdefault(f + "_page", None)
        if entry["operation"] is None:
            # one instruction (see verify) carries no Operation block; keep the trailing prose as description
            pass
        if diag:
            missing = [f for f in FIELD_RE if not entry[f]]
            if missing:
                print(f"  [diag] {name} (p{pno}) missing: {missing}", file=sys.stderr)
        out.append(entry)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="sources/esp32-s3_technical_reference_manual_en.pdf")
    ap.add_argument("--corpus", default="corpus/trm-s3")
    ap.add_argument("--out", default="data")
    ap.add_argument("--diag", action="store_true")
    a = ap.parse_args()

    from pypdf import PdfReader
    reader = PdfReader(a.pdf)
    pages = [json.loads(l)["text"] for l in open(os.path.join(a.corpus, "pages.jsonl"), encoding="utf-8")]

    os.makedirs(a.out, exist_ok=True)
    secs = sections_from_outline(a.corpus, SEC_FIRST, SEC_LAST)
    print(f"sections from outline: {len(secs)}")
    insts = extract_instructions(pages, secs, diag=a.diag)
    json.dump(insts, open(os.path.join(a.out, "pie_instructions.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"pie_instructions.json: {len(insts)} entries, "
          f"missing fields: {sum(1 for e in insts for f in FIELD_RE if not e[f])}")

    table = extract_table(reader, diag=a.diag)
    json.dump(table, open(os.path.join(a.out, "pie_pipeline.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"pie_pipeline.json: {len(table)} rows, "
          f"rows with no operands at all: {sum(1 for r in table if not r['operands_use'] and not r['operands_def'] and not r['special_regs_use'] and not r['special_regs_def'])}")

    parts = ["# TRM 1.7 Instruction Performance (verbatim, with page markers)\n",
             "Source: ESP32-S3 TRM v1.8, printed pages %d-%d. Printed page == PDF page.\n"
             % (HAZARD_FIRST, HAZARD_LAST)]
    for p in range(HAZARD_FIRST, HAZARD_LAST + 1):
        parts.append(f"\n<!-- page {p} -->\n")
        parts.append("\n".join(l for l in pages[p - 1].split("\n") if not NOISE.match(l.strip())))
    open(os.path.join(a.out, "pie_hazards.md"), "w", encoding="utf-8").write("".join(parts))
    print("pie_hazards.md written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
