#!/usr/bin/env python3
"""Build and query a searchable local corpus from a large PDF (vendor manual, datasheet, standard).

Why: a 1000+ page reference document is not read linearly. Extract every page to JSONL once (greps are
instant afterwards), keep the bookmark tree, then navigate by page number.

    python pdf_corpus.py build manual.pdf [--out DIR]        # -> DIR/pages.jsonl + DIR/outline.json
    python pdf_corpus.py toc   --corpus DIR [PATTERN] [--depth N] [--max N]
    python pdf_corpus.py find  --corpus DIR REGEX            # bookmark titles = fastest index
    python pdf_corpus.py text  --corpus DIR PAGE[-PAGE] [--chars N]
    python pdf_corpus.py grep  --corpus DIR REGEX [--max N]

Dependency: pypdf only. On PEP 668 systems (externally managed python):
    python3 -m venv .venv && .venv/bin/pip install pypdf && .venv/bin/python pdf_corpus.py build ...

`build` prints a verification report: page count, pages whose printed label differs from the physical
page number (so you know whether "printed page == PDF page"), and near-empty pages (a sign of a scanned
page or a font-encoding extraction problem).
"""
import argparse
import json
import os
import re
import sys

NEAR_EMPTY = 40
_cache = {}


def paths(corpus):
    return os.path.join(corpus, "pages.jsonl"), os.path.join(corpus, "outline.json")


def load_pages(corpus):
    key = ("pages", corpus)
    if key not in _cache:
        pages_file, _ = paths(corpus)
        _cache[key] = [json.loads(line) for line in open(pages_file, encoding="utf-8")]
    return _cache[key]


def load_outline(corpus):
    _, outline_file = paths(corpus)
    return json.load(open(outline_file, encoding="utf-8"))


def cmd_build(a):
    from pypdf import PdfReader

    pdf = a.pdf
    out = a.out or os.path.dirname(os.path.abspath(pdf)) or "."
    os.makedirs(out, exist_ok=True)
    reader = PdfReader(pdf)
    try:
        labels = list(reader.page_labels)
    except Exception:
        labels = []

    pages_file, outline_file = paths(out)
    near_empty = []
    total = 0
    with open(pages_file, "w", encoding="utf-8") as fh:
        for total, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # one bad page must not kill the build
                text = "[extract error: %s]" % exc
            label = labels[total - 1] if total - 1 < len(labels) else None
            if len(text.strip()) < NEAR_EMPTY:
                near_empty.append(total)
            fh.write(json.dumps({"pdf_page": total, "label": label, "text": text}, ensure_ascii=False) + "\n")

    outline = []

    def walk(items, depth=0):
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            try:
                page_no = reader.get_destination_page_number(item) + 1
            except Exception:
                page_no = None
            outline.append({"depth": depth, "title": str(item.title).strip(), "pdf_page": page_no})

    try:
        walk(reader.outline)
    except Exception as exc:
        print("outline unavailable: %s" % exc, file=sys.stderr)
    json.dump(outline, open(outline_file, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    mismatch = [(r["pdf_page"], r["label"]) for r in load_pages(out)
                if r["label"] and str(r["pdf_page"]) != str(r["label"])]
    print("pages=%d  outline_entries=%d  near_empty=%d" % (total, len(outline), len(near_empty)))
    print("label!=page: %d %s" % (len(mismatch), mismatch[:10]))
    print("near-empty pages: %s" % (near_empty[:40],))
    if not mismatch:
        print("-> printed page number equals PDF page number; no offset arithmetic needed")
    print("corpus: %s" % pages_file)


def cmd_toc(a):
    pat = re.compile(a.pattern, re.I) if a.pattern else None
    shown = 0
    for entry in load_outline(a.corpus):
        if entry["depth"] > a.depth:
            continue
        if pat and not pat.search(entry["title"]):
            continue
        print("%5sp  %s%s" % (entry["pdf_page"], "  " * entry["depth"], entry["title"]))
        shown += 1
        if shown >= a.max:
            break
    print("--- %d entries" % shown)


def cmd_find(a):
    pat = re.compile(a.pattern, re.I)
    hits = [e for e in load_outline(a.corpus) if pat.search(e["title"])]
    for entry in hits[: a.max]:
        print("%5sp  %s%s" % (entry["pdf_page"], "  " * entry["depth"], entry["title"]))
    print("--- %d title hits" % len(hits))


def cmd_text(a):
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", a.pages)
    if not m:
        sys.exit("page spec must be N or N-M")
    lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
    for row in load_pages(a.corpus):
        if lo <= row["pdf_page"] <= hi:
            body = row["text"]
            if len(body) > a.chars:
                body = body[: a.chars] + "\n...[truncated; raise --chars]"
            print("\n===== p%s (printed %s) =====" % (row["pdf_page"], row.get("label")))
            print(body)


def cmd_grep(a):
    pat = re.compile(a.pattern, re.I)
    hits = 0
    for row in load_pages(a.corpus):
        if pat.search(row["text"]):
            print("p%s (printed %s)" % (row["pdf_page"], row.get("label")))
            hits += 1
            if hits >= a.max:
                break
    print("--- %d page hits (cap %d)" % (hits, a.max))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="extract pages + outline into a corpus directory")
    b.add_argument("pdf")
    b.add_argument("--out", default=None, help="corpus dir (default: the PDF's own directory)")
    b.set_defaults(fn=cmd_build)

    t = sub.add_parser("toc", help="list bookmark entries")
    t.add_argument("--corpus", required=True)
    t.add_argument("pattern", nargs="?", default=None)
    t.add_argument("--depth", type=int, default=1)
    t.add_argument("--max", type=int, default=400)
    t.set_defaults(fn=cmd_toc)

    f = sub.add_parser("find", help="match bookmark titles")
    f.add_argument("--corpus", required=True)
    f.add_argument("pattern")
    f.add_argument("--max", type=int, default=60)
    f.set_defaults(fn=cmd_find)

    x = sub.add_parser("text", help="print page text")
    x.add_argument("--corpus", required=True)
    x.add_argument("pages")
    x.add_argument("--chars", type=int, default=14000)
    x.set_defaults(fn=cmd_text)

    g = sub.add_parser("grep", help="find pages containing a regex")
    g.add_argument("--corpus", required=True)
    g.add_argument("pattern")
    g.add_argument("--max", type=int, default=30)
    g.set_defaults(fn=cmd_grep)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
