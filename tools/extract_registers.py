#!/usr/bin/env python3
"""Extract the TRM's register knowledge into machine-readable form.

Two artifacts, both keyed to the printed page they came from:

  data/registers.json         <- every "Register Summary" table in the TRM (46 sections, chapters 2-39):
                                 register name, description, offset as printed, access type, the group label it
                                 sits under, and the section it belongs to.
  data/peripheral_map.json    <- Table 4.3-3 "Module/Peripheral Address Mapping" (p408-409): target, low and
                                 high address, size, notes. Register offsets are relative to these bases
                                 (each Register Summary says so in its preamble), so this is what turns an
                                 offset into an absolute address.

Layout facts this relies on (measured from the PDF, not assumed):

- A Register Summary is a 4-column table: Name | Description | Address | Access. The header row repeats on
  every page, so column origins are read per page from the header labels themselves -- chapters disagree on
  x (Description starts at 217, 246 or 257).
- **Rows are anchored by the register name on the same text line as its address**; the only thing that spills
  into the next line is a wrapped description.
- **A register name can be split into two words** by a font change inside the name ("SENS_" + "SAR_COCPU_
  INT_RAW_REG" on p332), so names are rebuilt from every Name-column word on the line.
- Group labels ("ULP Timer Registers", "Configuration Registers") are Name-column lines with no address on
  them, and they wrap over two lines.
- A page can carry more than one table: p878 has 20.4's "Memory Blocks" table (Name | Description | Size |
  Starting Address | Ending Address | Access) above the start of 20.5's register summary. Only headers whose
  labels run Name < Description < Address < Access, with a bare "Address", are register summaries.
- Several subsections share one page (2.9.1-2.9.4 all start on p332), so each page is parsed once and rows
  are attributed to the numbered subsection heading ("2.9.1") above them.
- Names may be parameterised: "GDMA_IN_LINK_CHn_REG (n: 0-4)" with an offset expression "(0x0020+192*n)";
  those keep the expression verbatim and are flagged.

Usage: .venv/bin/python tools/extract_registers.py [--pdf PATH] [--corpus DIR] [--out DIR] [--diag]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*_REG$")
NAME_PARAM_RE = re.compile(r"^[A-Z][A-Z0-9_]*_REG\s*\(n:\s*[\d\-]+\)$")
HEX_RE = re.compile(r"^0x[0-9A-Fa-f]{2,8}$")
HEX_UNDERSCORE_RE = re.compile(r"^0x[0-9A-Fa-f]{4}_[0-9A-Fa-f]{4}$")
OFFSET_EXPR_RE = re.compile(r"^\((0x[0-9A-Fa-f]+)([+\-]\d+\*n)?\)$")
SUBSEC_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
ACCESS_TOKENS = {"R/W", "RO", "WO", "varies", "R/W1C", "R/W1S", "R/W1T", "R/W1C/SS", "R/WTC/SS",
                 "R/W1S/SS", "R/W1T/SS", "RW", "read-only", "write-only"}
HEADER_LABELS = ("Name", "Description", "Address", "Access")
NOT_REGISTER_ADDRESS = {"Starting", "Ending", "Low", "High", "Boundary"}
FOOTER_RE = re.compile(r"^(Espressif Systems \d+|Submit Documentation Feedback|"
                       r"ESP32-S3 TRM \(Version [\d.]+\)|GoBack)$")
CHAPTER_RE = re.compile(r"^Chapter \d+")

ROW_TOL = 4.0       # same-line tolerance for words
PITCH = 15.9        # table row pitch in points


def words_of(page) -> list[tuple[float, float, str]]:
    """(x, y, word) for every word on the page, top-to-bottom then left-to-right.

    Pages whose table is printed landscape carry /Rotate 90 and PyMuPDF reports the words in the *unrotated*
    frame, where every word's box is tall and narrow (y up to the unrotated height, 841.89 > the visible 595).
    Reading them as-is puts every cell of a row in one column. The rotation is therefore undone here, using
    the far y edge as the rotated x -- verified by the column labels landing on the same x as the data
    (e.g. ch.9's interrupt tables, p547-554).
    """
    rotated = bool(page.rotation) and page.rotation % 180 == 90
    h = page.mediabox.height if rotated else 0.0
    out = []
    for w in page.get_text("words"):
        text = w[4]
        if not text.strip() or FOOTER_RE.match(text) or CHAPTER_RE.match(text):
            continue
        if rotated:
            x, y = round(h - w[3], 1), round(w[0], 1)
            if not (55 < x < 800):        # the running head/footer run down the margins after rotation
                continue
        else:
            x, y = round(w[0], 1), round(w[1], 1)
            if not (45 < y < 775):        # running head above / footer below the text block
                continue
        out.append((x, y, text))
    out.sort(key=lambda f: (f[1], f[0]))
    return out


def column_of(x: float, cols: dict[str, float]) -> str:
    """The column whose label starts at or before x (header labels are left-aligned in their column)."""
    best, best_x = "Name", -1e9
    for label, cx in cols.items():
        if cx <= x + 2 and cx > best_x:
            best, best_x = label, cx
    return best


def header_columns(ws: list[tuple[float, float, str]]) -> list[tuple[float, dict[str, float]]]:
    """Every Register-Summary header row on the page, top to bottom: [(header_y, {label: x})]."""
    found: list[tuple[float, dict[str, float]]] = []
    for x, y, t in ws:
        if t != "Name":
            continue
        # The four labels may sit on slightly different baselines (p498: "Name" 10.8pt below the rest), so the
        # band is one row pitch wide -- the x-order check below is what keeps unrelated text out.
        band = sorted([w for w in ws if abs(w[1] - y) < PITCH], key=lambda f: f[0])
        labels_xy = {t2: (x2, y2) for x2, y2, t2 in band if t2 in HEADER_LABELS}
        # Only Name + Description are required. Chapters vary beyond that: some print an "Address" column, the
        # I2S chapter prints two ("I2S0 Ad-/dress", "I2S1 Ad-/dress" -- hyphenated into two words), and the
        # address itself is located by shape anyway.
        if not {"Name", "Description"} <= set(labels_xy):
            continue
        if any(t2 in NOT_REGISTER_ADDRESS for _x2, _y2, t2 in band):
            continue                       # a memory-block table (Name|Size|Starting Address|Ending Address)
        labels = {k: v[0] for k, v in labels_xy.items()}
        if labels["Name"] >= labels["Description"]:
            continue
        header_y = max(v[1] for v in labels_xy.values())
        found.append((header_y, labels))
    found.sort()
    # One header per y band: the same labels can be reached from several "Name" words in the band.
    dedup: list[tuple[float, dict[str, float]]] = []
    for y2, labels2 in found:
        if dedup and abs(dedup[-1][0] - y2) < PITCH:
            continue
        dedup.append((y2, labels2))
    return dedup


def group_lines(ws, cols, col: str, tol: float = ROW_TOL) -> list[tuple[float, str]]:
    """Join the words of one column into per-text-line strings: [(y, text)]."""
    out: list[list] = []
    for x, y, t in sorted([w for w in ws if column_of(w[0], cols) == col], key=lambda f: (f[1], f[0])):
        if out and abs(y - out[-1][0]) <= tol:
            out[-1][1].append(t)
        else:
            out.append([y, [t]])
    return [(y, " ".join(parts)) for y, parts in out]


def subheadings(ws) -> list[tuple[float, str]]:
    """(y, '2.9.1') for every numbered heading on the page, top to bottom."""
    out: list[tuple[float, str]] = []
    for x, y, t in ws:
        if x < 75 and SUBSEC_RE.match(t):
            if not out or abs(out[-1][0] - y) > 3:
                out.append((y, t))
    return sorted(out)


def looks_like_name_part(text: str) -> bool:
    """A Name-column line that is part of a register name (possibly truncated by the cell width)."""
    squeezed = re.sub(r"\s+", "", text)
    if NAME_RE.match(squeezed) or NAME_PARAM_RE.match(squeezed):
        return True
    if squeezed.endswith("_") or squeezed.startswith("_"):
        return True
    return bool(re.match(r"^[A-Z][A-Z0-9_]*$", squeezed)) and len(squeezed) > 6


def assemble_name(parts: list[str]) -> str | None:
    """Join the lines of one register's name: font splits and wraps must not become separate registers."""
    if not parts:
        return None
    out = parts[0]
    for nxt in parts[1:]:
        if NAME_RE.match(out) and NAME_RE.match(re.sub(r"\s+", "", nxt)):
            break                      # the band caught the next row's name too; keep only this one
        if out.endswith("_") or nxt.startswith("_") or re.match(r"^[A-Z0-9_]+$", nxt):
            out += nxt
        else:
            out += " " + nxt
    out = re.sub(r"\s+", " ", out).strip()
    squeezed = re.sub(r"\s+", "", out)
    # A font change inside one name leaves it as two words ("SENS_ SAR_COCPU_INT_RAW_REG").
    return squeezed if (NAME_RE.match(squeezed) or NAME_PARAM_RE.match(squeezed)) else out


def name_blocks(name_lines: list[tuple[float, str]]) -> list[tuple[float, float, str]]:
    """Group Name-column lines into register names: [(first_y, last_y, name)].

    A name arrives as one line, as two words on one line, or as two lines when it does not fit its cell
    (that second line may sit *below* the row's address line -- p828, ch.9). Grouping by "does the text
    complete a register name yet, and is the next line close enough to continue it" handles all three, which
    a band-around-the-address does not.
    """
    blocks: list[tuple[float, float, str]] = []
    cur: list[tuple[float, str]] = []
    for y, text in name_lines:
        if not looks_like_name_part(text):
            continue
        if cur and y - cur[-1][0] > PITCH * 1.4:
            blocks.append((cur[0][0], cur[-1][0], assemble_name([t for _y, t in cur]) or ""))
            cur = []
        cur.append((y, text))
        joined = assemble_name([t for _y, t in cur]) or ""
        if NAME_RE.match(joined) or NAME_PARAM_RE.match(joined):
            blocks.append((cur[0][0], cur[-1][0], joined))
            cur = []
    if cur:
        blocks.append((cur[0][0], cur[-1][0], assemble_name([t for _y, t in cur]) or ""))
    return blocks


def parse_one_table(table, cols, pno, heads, owners, chapter, diag=False, carry_in=None) -> list[dict]:
    """One Register Summary table -> rows, anchored by name blocks with their address located by proximity.

    The row's name is the anchor (a register exists because the manual lists its name); the address line is
    found next to it, whether it sits above, on, or below the name -- wrapped names move it around.
    """
    rows: list[dict] = []
    name_lines = group_lines(table, cols, "Name")
    groups: list[tuple[float, str]] = [
        (y, text) for y, text in name_lines
        if not looks_like_name_part(text) and 3 < len(text) < 60 and text[0].isupper()
        and not text.startswith(("(", "0x"))]

    # Address-shaped tokens anywhere right of the Name column, rightmost token wins on a line.
    addrs: list[tuple[float, float, str]] = []
    for x, y, t in sorted(table, key=lambda f: (f[1], f[0])):
        if x <= cols.get("Description", cols["Name"]) - 10 or not (
                HEX_RE.match(t) or HEX_UNDERSCORE_RE.match(t) or OFFSET_EXPR_RE.match(t)):
            continue
        if addrs and abs(addrs[-1][0] - y) < ROW_TOL:
            continue                       # keep the first (leftmost) address column of the row
        addrs.append((y, x, t))
    if not addrs:
        return rows

    for first_y, last_y, name in name_blocks(name_lines):
        centre = (first_y + last_y) / 2
        near = [a for a in addrs if first_y - PITCH <= a[0] <= last_y + PITCH]
        if not near:
            if diag:
                print(f"  [diag] p{pno} {name!r}: no address near y={centre}", file=sys.stderr)
            continue
        ay, _ax, token = min(near, key=lambda a: abs(a[0] - centre))
        band = [w for w in table if centre - PITCH * 0.8 <= w[1] <= centre + PITCH * 0.8]
        skip = {part for _y, part in name_lines if part in name}
        access = min((t for x, y, t in band if t in ACCESS_TOKENS),
                     key=lambda t: abs(next(y for _x, y, tt in band if tt is t) - centre), default=None)
        desc = " ".join(t for x, y, t in sorted(band, key=lambda f: (f[1], f[0]))
                        if t not in HEADER_LABELS and t != token and t not in ACCESS_TOKENS
                        and t not in {part for part in name.split()})
        expr = token if OFFSET_EXPR_RE.match(token) else None
        sub = next((h for hy, h in reversed(heads) if hy < first_y), carry_in)
        title = next((o["title"] for o in owners
                      if sub and o["title"].strip().startswith(sub + " ")), owners[0]["title"])
        rows.append({
            "name": name,
            "description": re.sub(r"\s+", " ", desc).strip(),
            "offset": None if expr else token,
            "offset_expr": expr,
            "offset_is_parameterised": bool(expr) or bool(NAME_PARAM_RE.match(name)),
            "address_is_absolute": bool(HEX_UNDERSCORE_RE.match(token)),
            "access": access,
            "group": next((g for gy, g in reversed(groups) if gy < first_y), None),
            "section": " ".join(title.split()),
            "subsection": sub,
            "chapter": chapter,
            "source_page": pno,
        })
        if diag and access is None:
            print(f"  [diag] p{pno} {name}: no access token", file=sys.stderr)

    return rows


def name_vocabulary(corpus: str) -> dict[str, str]:
    """Canonical register names as spelled in the manual, keyed with non-alphanumerics removed.

    Some cells lose an underscore in the text layer ("TWAI_ARB LOST CAP REG" for TWAI_ARB_LOST_CAP_REG) and
    some miss part of the name; the same manual spells those registers correctly in its per-register detail
    headings and cross-references, so the repair stays inside the primary source.
    """
    import collections
    words: collections.Counter = collections.Counter()
    for line in open(os.path.join(corpus, "pages.jsonl"), encoding="utf-8"):
        for w in re.findall(r"[A-Z][A-Z0-9_]*_REG\b", line):
            words[w] += 1
    return {re.sub(r"[^A-Z0-9]", "", w): w for w in sorted(words)}


def repair_name(name: str, vocab: dict[str, str]) -> str | None:
    """Map a damaged name onto the manual's canonical spelling, or return None.

    Trims trailing junk words one at a time ("GPIO_SIGMADELTA_MISC_REG MISC"), so a cell whose text layer
    swallowed a word boundary still lands on the right register instead of a new one.
    """
    words = name.split()
    if not words:
        return None
    for cut in range(len(words), 0, -1):
        cand = " ".join(words[:cut])
        key = re.sub(r"[^A-Z0-9]", "", cand.upper())
        if key in vocab:
            return vocab[key]
        if cand.endswith("_"):
            key2 = re.sub(r"[^A-Z0-9]", "", (cand + "REG").upper())
            if key2 in vocab:
                return vocab[key2]
    return None


def parse_summary_pages(doc, ranges: list[dict], diag: bool = False,
                        vocab: dict[str, str] | None = None) -> list[dict]:
    """Parse every page that carries a Register Summary table (each page once), then dedupe."""
    pages: dict[int, dict] = {}
    for r in ranges:
        for pno in range(r["first"], r["last"] + 1):
            entry = pages.setdefault(pno, {"owners": [], "chapter": r["chapter"]})
            entry["owners"].append(r)

    rows: list[dict] = []
    carry: dict[object, str | None] = {}
    carry_cols: dict[object, tuple[float, dict[str, float]]] = {}
    for pno in sorted(pages):
        info = pages[pno]
        key = info["owners"][0]["title"]
        ws = words_of(doc[pno - 1])
        heads = subheadings(ws)
        if heads:
            carry[key] = heads[-1][1]          # the table continues under this heading on later pages
        headers = header_columns(ws)
        if not headers and key in carry_cols:
            # The table spilled onto a page that does not repeat the column header (6.14.2 IO MUX: header on
            # p497, rows on p498). Reuse the last known columns; prose cannot create rows because a row needs
            # an upper-case name line and an address-shaped token.
            headers = [carry_cols[key]]
        if not headers:
            continue
        carry_cols[key] = headers[-1]
        for i, (hy, cols) in enumerate(headers):
            bottom = headers[i + 1][0] - 2 if i + 1 < len(headers) else 1e9
            # Nothing to the left of the Name column belongs to the table: on a rotated page the running
            # footer lands there (x' < 80) and its words otherwise merge into wrapped register names.
            table = [w for w in ws if hy + 2 < w[1] < bottom and w[0] >= cols["Name"] - 5]
            if table:
                rows.extend(parse_one_table(table, cols, pno, heads, info["owners"], info["chapter"],
                                            diag, carry_in=carry.get(key)))

    if vocab:
        fixed = 0
        for r in rows:
            if NAME_RE.match(r["name"]):
                continue
            rep = repair_name(r["name"], vocab)
            if rep:
                r["name_repaired_from"] = r["name"]
                r["name"] = rep
                fixed += 1
            elif diag:
                print(f"  [diag] p{r['source_page']} name not repairable: {r['name']!r}", file=sys.stderr)
        if fixed:
            print(f"repaired {fixed} name(s) against the manual's own spelling")

    seen, unique = set(), []
    for r in rows:
        key = (r["source_page"], r["name"], r["offset"] or r["offset_expr"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


def parse_peripheral_map(doc, first: int, last: int, diag: bool = False) -> list[dict]:
    """Table 4.3-3: target name + low/high boundary addresses + size + notes, one row per low address."""
    out: list[dict] = []
    for pno in range(first, last + 1):
        ws = [w for w in words_of(doc[pno - 1]) if w[1] > 100]   # below the two-line column header
        lows = sorted([(y, x, t) for x, y, t in ws if 200 <= x < 330 and HEX_UNDERSCORE_RE.match(t)])
        for i, (ly, _lx, low) in enumerate(lows):
            next_y = lows[i + 1][0] if i + 1 < len(lows) else 1e9
            band = sorted([w for w in ws if ly - 8 <= w[1] < next_y - 8], key=lambda f: (f[1], f[0]))
            target = " ".join(t for x, y, t in band if x < 200 and y <= ly + 2)
            high = next((t for x, y, t in band if 330 <= x < 410 and HEX_UNDERSCORE_RE.match(t)), None)
            size = next((t for x, y, t in band if 400 <= x < 470 and re.match(r"^\d+$", t)), None)
            if size is None:      # not a table row (the preamble line above the table has the same shape)
                continue
            notes = " ".join(t for x, y, t in band if x >= 470)
            out.append({"target": target.strip(), "low_address": low, "high_address": high,
                        "size_kb": int(size) if size else None, "notes": notes.strip() or None,
                        "source_page": pno})
            if diag and not target:
                print(f"  [diag] p{pno} row {low}: empty target", file=sys.stderr)
    return out


def summary_sections(corpus: str) -> list[dict]:
    """Outline entries ending in 'Register Summary', each with the page range it owns."""
    outl = json.load(open(os.path.join(corpus, "outline.json"), encoding="utf-8"))
    flat = sorted(outl, key=lambda e: (e["pdf_page"], e["depth"]))
    ranges = []
    for e in [x for x in outl if x["title"].endswith("Register Summary")]:
        later = [o["pdf_page"] for o in flat if o["pdf_page"] > e["pdf_page"]]
        last = (min(later) - 1) if later else e["pdf_page"]
        chapter = re.match(r"^(\d+)\.", e["title"])
        ranges.append({"title": e["title"], "first": e["pdf_page"], "last": last,
                       "chapter": int(chapter.group(1)) if chapter else None})
    return ranges


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="sources/esp32-s3_technical_reference_manual_en.pdf")
    ap.add_argument("--corpus", default="corpus/trm-s3")
    ap.add_argument("--out", default="data")
    ap.add_argument("--diag", action="store_true")
    a = ap.parse_args()

    import pymupdf
    doc = pymupdf.open(a.pdf)
    secs = summary_sections(a.corpus)
    print(f"Register Summary sections: {len(secs)}")
    rows = parse_summary_pages(doc, secs, diag=a.diag, vocab=name_vocabulary(a.corpus))
    rows.sort(key=lambda r: (r["chapter"] or 0, r["source_page"], r["name"]))
    os.makedirs(a.out, exist_ok=True)
    json.dump(rows, open(os.path.join(a.out, "registers.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    no_off = [r for r in rows if not r["offset"] and not r["offset_expr"]]
    print(f"registers.json: {len(rows)} rows, {len({r['name'] for r in rows})} distinct names, "
          f"{len({r['section'] for r in rows})} sections, {len(no_off)} without an offset")
    for r in no_off[:10]:
        print(f"    no offset: {r['name']} (p{r['source_page']}, {r['section']})")

    pmap = parse_peripheral_map(doc, 408, 409, diag=a.diag)
    json.dump(pmap, open(os.path.join(a.out, "peripheral_map.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"peripheral_map.json: {len(pmap)} rows "
          f"({sum(1 for r in pmap if r['high_address'])} with a high address, "
          f"{sum(1 for r in pmap if r['target'])} with a target name)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
