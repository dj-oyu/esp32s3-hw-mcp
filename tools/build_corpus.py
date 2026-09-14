#!/usr/bin/env python3
"""Build the page corpus for a vendor PDF: corpus/<name>/pages.jsonl + outline.json.

Same schema as the pypdf helper this project started from, but the text comes from PyMuPDF, which inserts
a space where the font changes. pypdf joins those runs instead ("registerqa." for "register qa."), which
silently breaks word-level search over the corpus — e.g. grep for "register qa" misses every hit.

    .venv/bin/python tools/build_corpus.py sources/esp32-s3_technical_reference_manual_en.pdf --out corpus/trm-s3

Each line: {"pdf_page": 65, "label": "65", "text": "..."}; outline entries: {"depth", "title", "pdf_page"}.
`label` is the page number printed on the page, so callers can state whether an offset is needed.
"""
from __future__ import annotations

import argparse
import json
import os


def build(pdf: str, out: str) -> int:
    import pymupdf

    doc = pymupdf.open(pdf)
    os.makedirs(out, exist_ok=True)
    pages_file = os.path.join(out, "pages.jsonl")
    near_empty, mismatched = [], []
    with open(pages_file, "w", encoding="utf-8") as fh:
        for i, page in enumerate(doc):
            text = page.get_text()
            try:
                label = page.get_label() or str(i + 1)
            except Exception:
                label = str(i + 1)
            if label != str(i + 1):
                mismatched.append((i + 1, label))
            if len(text.strip()) < 40:
                near_empty.append(i + 1)
            fh.write(json.dumps({"pdf_page": i + 1, "label": label, "text": text}, ensure_ascii=False) + "\n")

    toc = [{"depth": lvl - 1, "title": title.strip(), "pdf_page": pno} for lvl, title, pno in doc.get_toc()]
    json.dump(toc, open(os.path.join(out, "outline.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"pages={len(doc)}  outline_entries={len(toc)}  near_empty={len(near_empty)}")
    print(f"label != page: {len(mismatched)} {mismatched[:5]}")
    if not mismatched:
        print("-> printed page number equals PDF page number; no offset arithmetic needed")
    if near_empty:
        print(f"-> near-empty pages (scanned page or extraction failure?): {near_empty[:20]}")
    print(f"-> {pages_file}")
    return len(near_empty)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    build(a.pdf, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
