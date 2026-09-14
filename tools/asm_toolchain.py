#!/usr/bin/env python3
"""Assemble Xtensa snippets with the real toolchain, and check the manual's encodings against it.

Why this exists
---------------
The Technical Reference Manual prints each PIE instruction as a bit-field diagram, and those diagrams are
what the extraction turns into `instruction_word` strings such as

    11 / qa[2:1] / 1101 / qa[0] / 011 / qy[2:1] / 00 / qx[2:1] / qy[0] / qx[0] / 0100

Reading such a diagram by eye is exactly where a mistake silently becomes wrong firmware: a transposed
field or a wrong bit width still assembles to *something*. So the manual is never trusted alone. The
toolchain that builds the firmware (`xtensa-esp32s3-elf-as` / `objdump`, the same Espressif binutils that
`idf.py build` uses) is the authority for what a mnemonic actually encodes to, and every manual-derived
claim is checked against it here.

Three operations, each grounded in tool output rather than in this file's opinion:

  assemble(snippet)            what does the toolchain encode for these instructions?
  encode_from_manual(name, ..)  what does the manual's diagram say the encoding is?
  check_instruction(...)        do those two agree? if not, which field disagrees?
  decode(word_hex)              what instruction is this word, according to the disassembler?

Byte order, which is easy to get backwards: the assembler listing prints bytes in memory order
(`ld.qr q0, a3, 0` -> 34 20 CD) while the manual's diagram and `objdump` print the instruction word as a
number, most significant bit first (CD2034). Everything here reports the *word* form, because that is what
the diagram shows.

    .venv/bin/python tools/asm_toolchain.py --check-asm "ld.qr q0, a3, 0"
    .venv/bin/python tools/asm_toolchain.py --decode cd2034
    .venv/bin/python tools/asm_toolchain.py --check-all --limit 20
    .venv/bin/python tools/asm_toolchain.py --check-all --json out.json     # full sweep
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTRUCTIONS = os.path.join(ROOT, "data", "pie_instructions.json")

# Where the Espressif binutils lives. PATH first (after `source /opt/esp-idf/export.sh`), then the
# managed-install layout that esp-idf writes on first use.
TOOL_DIRS = [
    os.environ.get("XTENSA_TOOLCHAIN_DIR", ""),
    "/root/.espressif/tools/xtensa-esp-elf/*/xtensa-esp-elf/bin",
    os.path.expanduser("~/.espressif/tools/xtensa-esp-elf/*/xtensa-esp-elf/bin"),
    "/opt/esp-idf/tools/xtensa-esp-elf/*/xtensa-esp-elf/bin",
]
TOOL_PREFIX = os.environ.get("XTENSA_TOOL_PREFIX", "xtensa-esp32s3-elf-")

# A listing line: "   5 0003 3420CD   \t    ld.qr q0, a3, 0" -- the address and the encoded bytes are
# both optional (a source line need not emit code).
LISTING_RE = re.compile(r"^\s*(\d+)(?:\s+([0-9a-f]{4,8}))?(?:\s+([0-9A-F]{2,16}))?\s*$")
FIELD_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_]*)\[(\d+)(?::(\d+))?\]$")
BITS_RE = re.compile(r"^[01]+$")


def find_tool(name: str) -> str | None:
    """Absolute path of a toolchain program, or None. Never guesses: a missing toolchain is reported."""
    exe = TOOL_PREFIX + name
    found = shutil.which(exe)
    if found:
        return found
    for pattern in TOOL_DIRS:
        if not pattern:
            continue
        for d in sorted(glob.glob(pattern)):
            cand = os.path.join(d, exe)
            if os.path.exists(cand):
                return cand
    return None


def toolchain_info() -> dict:
    as_bin, objdump = find_tool("as"), find_tool("objdump")
    info: dict = {"assembler": as_bin, "objdump": objdump, "version": None, "available": bool(as_bin)}
    if as_bin:
        try:
            out = subprocess.run([as_bin, "--version"], capture_output=True, text=True, timeout=30).stdout
            info["version"] = out.splitlines()[0].strip() if out else None
        except Exception as exc:                                    # pragma: no cover - environment
            info["version"] = f"<version probe failed: {exc}>"
    if not as_bin:
        info["hint"] = ("No Espressif assembler found. Source the ESP-IDF environment first "
                        "(`. /opt/esp-idf/export.sh`) or set XTENSA_TOOLCHAIN_DIR.")
    return info


def _bytes_to_word(byte_hex: str) -> str:
    """Memory-order bytes to the instruction word as a number, MSB first (what objdump prints)."""
    groups = [byte_hex[i:i + 2] for i in range(0, len(byte_hex), 2)]
    return "".join(reversed(groups)).lower()


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def assemble(snippet: str, *, raw: bool = False, symbol: str = "__check_asm") -> dict:
    """Assemble `snippet` and report, per source line, the bytes and instruction word the toolchain chose.

    `raw=False` (default) treats the snippet as the *body* of a function: it is wrapped in a text
    section with an entry/retw.n pair so the assembler sees the same calling convention the firmware
    uses. `raw=True` assembles the snippet verbatim (for a complete file).
    """
    info = toolchain_info()
    if not info["available"]:
        return {"accepted": False, "error": "toolchain_missing", **info}

    body = snippet if raw else "\n".join(
        ["    .text", "    .align 4", f"    .global {symbol}", f"{symbol}:", "    entry a1, 32",
         snippet.rstrip("\n"), "    retw.n", ""])
    if raw:
        body = snippet

    with tempfile.TemporaryDirectory() as td:
        src, obj = os.path.join(td, "snippet.S"), os.path.join(td, "snippet.o")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(body)
        # -al: write a listing that pairs every source line with the encoding the assembler produced.
        proc = _run([info["assembler"], "-al", "-o", obj, src])
    out = {"accepted": proc.returncode == 0, "returncode": proc.returncode,
           "assembler": info["assembler"], "version": info["version"], "source": body}
    if proc.returncode != 0:
        out["stderr"] = proc.stderr.strip()
        out["errors"] = [l for l in proc.stderr.splitlines() if "Error" in l or "Warning" in l]
        return out

    instructions, wrapper_lines = [], set()
    for raw_line in proc.stdout.splitlines():
        if "\t" not in raw_line:
            continue
        left, text = raw_line.split("\t", 1)
        m = LISTING_RE.match(left)
        if not m:
            continue
        lineno, addr, byte_hex = int(m.group(1)), m.group(2), m.group(3)
        if not byte_hex:                        # a source line with no code
            continue
        instructions.append({
            "line": lineno,
            "address": f"0x{addr}" if addr else None,
            "bytes_memory": byte_hex.lower(),
            "word": _bytes_to_word(byte_hex),
            "source": text.strip(),
        })
    # The wrapper's own two instructions are identified by their text so a caller can ignore them.
    for ins in instructions:
        if ins["source"].startswith("entry ") or ins["source"].startswith("retw"):
            wrapper_lines.add(ins["line"])
    out["instructions"] = instructions
    out["body_instructions"] = [i for i in instructions if i["line"] not in wrapper_lines]
    return out


def decode(word_hex: str) -> dict:
    """Ask the disassembler what instruction this 16/24/32-bit word is. Word = number, MSB first."""
    word = re.sub(r"[\s_]", "", word_hex).lower()
    if not re.fullmatch(r"[0-9a-f]{2,16}", word) or len(word) % 2:
        return {"error": "not_a_hex_word", "input": word_hex}
    info = toolchain_info()
    if not info["available"]:
        return {"error": "toolchain_missing", **info}
    groups = [word[i:i + 2] for i in range(0, len(word), 2)]
    memory_order = ", ".join(f"0x{g}" for g in reversed(groups))
    source = f"    .text\n    .align 4\n    .global __decoded\n__decoded:\n    .byte {memory_order}\n"
    with tempfile.TemporaryDirectory() as td:
        src, obj = os.path.join(td, "d.S"), os.path.join(td, "d.o")
        open(src, "w", encoding="utf-8").write(source)
        proc = _run([info["assembler"], "-o", obj, src])
        if proc.returncode != 0:
            return {"word": word, "error": "assembler_rejected", "stderr": proc.stderr.strip()}
        proc = _run([info["objdump"], "-d", "--no-show-raw-insn", obj])
    text = None
    for line in proc.stdout.splitlines():
        if re.match(r"^\s+0:\t", line):
            text = line.split("\t", 1)[1].strip()
            break
    return {"word": word, "bytes_memory": "".join(reversed(groups)).lower(), "disassembly": text,
            "objdump": info["objdump"]}


def parse_layout(layout: list[str]) -> tuple[list[dict], list[str]]:
    """Turn one `instruction_word` string into ordered fields, MSB first.

    Each element of `layout` is either a constant bit string or a `name[hi:lo]` field. Anything else is
    returned in `unsupported` -- the PDF extraction sometimes pulls prose into the diagram, and inventing
    a field for those would be worse than admitting the layout cannot be read.
    """
    fields, unsupported = [], []
    for token in layout:
        token = token.strip()
        if not token:
            continue
        if BITS_RE.match(token):
            fields.append({"kind": "const", "bits": token})
            continue
        m = FIELD_RE.match(token)
        if m:
            name, hi = m.group(1), int(m.group(2))
            lo = int(m.group(3)) if m.group(3) is not None else hi
            fields.append({"kind": "field", "token": token, "operand": name, "hi": hi, "lo": lo,
                           "width": hi - lo + 1})
            continue
        unsupported.append(token)
    return fields, unsupported


def encode_from_manual(fields: list[dict], operands: dict[str, int]) -> dict:
    """Substitute operand values into a parsed layout and return the instruction word."""
    bits, used = [], []
    for f in fields:
        if f["kind"] == "const":
            bits.append(f["bits"])
            continue
        value = operands.get(f["operand"])
        if value is None:
            return {"error": "missing_operand", "operand": f["operand"], "operands": sorted(operands)}
        field = (value >> f["lo"]) & ((1 << f["width"]) - 1)
        bits.append(format(field, f"0{f['width']}b"))
        used.append({"token": f["token"], "value": value, "field_value": field})
    bit_string = "".join(bits)
    return {"bits": bit_string, "bit_width": len(bit_string), "word": f"{int(bit_string, 2):0{len(bit_string) // 4}x}",
            "fields_used": used}


def syntax_operands(syntax: str) -> dict:
    """Operand names, kinds and value ranges from an `assembler_syntax` line.

    The syntax line documents ranges as extra comma-separated entries that belong to the operand before
    them ("LD.QR qu, as, imm, -128..112" means: imm ranges over -128..112), so those are folded back in.
    """
    m = re.match(r"^\s*([A-Za-z0-9_.]+)\s*(.*)$", syntax.strip())
    if not m:
        return {"mnemonic": None, "operands": []}
    mnemonic, rest = m.group(1), m.group(2)
    operands: list[dict] = []
    for item in [x.strip() for x in rest.split(",") if x.strip()]:
        rng = re.match(r"^(-?\d+)\s*\.\.\s*(-?\d+)$", item)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            # A range is attached to the operand before it only when that operand is the immediate it
            # constrains ("LD.QR qu, as, imm, -128..112"). After a register it is an operand of its own:
            # EE.MOVI.32.A is written `qs, au, 0..3`, and that third operand is the diagram's `sel4`.
            if operands and operands[-1]["base"] and IMM_RE.match(operands[-1]["base"]):
                operands[-1]["range"] = [lo, hi]
            else:
                operands.append({"token": item, "name": None, "base": None, "range": [lo, hi]})
            continue
        if re.fullmatch(r"-?\d+", item):
            # A literal number is an operand of its own unless it is the range of the immediate before it
            # ("EE.ST.ACCX.IP as, -512..508" is a range, "EE.SRS.ACCX au, as, 0" is a third operand).
            if operands and operands[-1]["base"] and IMM_RE.match(operands[-1]["base"]):
                operands[-1]["range"] = [int(item), int(item)]
            elif not operands:
                continue
            else:
                operands.append({"token": item, "name": None, "base": "literal", "range": None,
                                 "literal": int(item)})
            continue
        base = re.match(r"^[A-Za-z_]+", item)
        operands.append({"token": item, "name": item, "base": base.group(0) if base else item,
                         "range": None})
    return {"mnemonic": mnemonic, "operands": operands}


# Operand kinds. QR registers are 3-bit (q0..q7), address registers 4-bit (a0..a15), the PIE float
# registers 4-bit (f0..f15). Immediates are documented as byte offsets whose low bits are dropped --
# LD.QR's -128..112 in a 4-bit field is a 16-byte step -- so the scale is derived from the documented
# range and the field width instead of being assumed, and both ends of the range are exercised.
REGISTER_PREFIX = {"q": "q", "a": "a", "f": "f"}
IMM_RE = re.compile(r"^imm")
NUMERIC_RE = re.compile(r"^(sel|sar|upd)")


def operand_widths(fields: list[dict]) -> dict[str, int]:
    """Total bit width per operand name: immediates are split into several groups in the diagram."""
    widths: dict[str, int] = {}
    for f in fields:
        if f["kind"] == "field":
            widths[f["operand"]] = widths.get(f["operand"], 0) + f["width"]
    return widths


def match_operands(got: dict, fields: list[dict]) -> tuple[list[dict], list[str]]:
    """Pair every layout operand with the syntax operand it belongs to.

    Named operands match by name. A bare range in the syntax ("0..3") is an operand in its own right --
    EE.MOVI.32.A is written `qs, au, 0..3` while its diagram calls that third operand `sel4` -- so each
    unmatched range is given to the next still-unmatched selector/immediate operand of the diagram, in
    diagram order. Anything left over is reported rather than guessed.
    """
    order: list[str] = []
    for f in fields:
        if f["kind"] == "field" and f["operand"] not in order:
            order.append(f["operand"])
    names = [o["name"] for o in got["operands"] if o["name"]]
    matched: list[dict] = []
    used: set[str] = set()
    for operand in got["operands"]:
        if operand["name"]:
            matched.append({**operand, "layout_name": operand["name"]})
            used.add(operand["name"])
            continue
        candidate = next((n for n in order if n not in used and (IMM_RE.match(n) or NUMERIC_RE.match(n))),
                         None)
        if candidate is None:
            # A literal operand from the syntax (`EE.SRS.ACCX au, as, 0`) is fixed by the manual and has no
            # field of its own in the diagram; it is emitted as printed and needs no diagram operand.
            if operand.get("base") == "literal":
                matched.append({**operand, "layout_name": None})
                continue
            return matched, [f"no diagram operand left for the bare range {operand['token']}"]
        matched.append({**operand, "layout_name": candidate})
        used.add(candidate)
    return matched, sorted(set(order) - used)


# Operand kinds. QR registers are 3-bit (q0..q7), address registers 4-bit (a0..a15), the PIE float
# registers 4-bit (f0..f15). Immediates are documented as byte offsets whose low bits are dropped
# (LD.QR's -128..112 in a 4-bit field is a 16-byte step), so the step is derived from the documented
# range divided over the field's value count, and the field itself holds that step *signed*: -128 with a
# step of 16 is -8, i.e. 0b1000 in four bits. Both ends of the range are exercised, so a wrong step or a
# missing sign shows up as a mismatch instead of passing quietly.
REGISTER_PREFIX = {"q": "q", "a": "a", "f": "f"}
IMM_RE = re.compile(r"^imm")
NUMERIC_RE = re.compile(r"^(sel|sar|upd)")


def synthesise(matched: list[dict], widths: dict[str, int], variant: int) -> tuple[dict, list[str]]:
    """(diagram operand values, assembler operand text) for one operand set.

    Distinct operand names get distinct register numbers: the multi-register forms (EE.LDF.128.IP takes
    four of them) are rejected by the assembler with "multiple writes to the same register" if the same
    number is used twice, and a check that cannot even assemble proves nothing.
    """
    layout_values: dict[str, int] = {}
    rendered: list[str] = []
    assigned: dict[str, int] = {}
    for operand in matched:
        name, base = operand["layout_name"], (operand["base"] or operand["layout_name"])
        if name is None:                        # a literal operand from the syntax line: no field of its own
            rendered.append(str(operand.get("literal", 0)))
            continue
        width = widths[name]
        steps = (1 << width) - 1
        if base[:1] in REGISTER_PREFIX:
            if name not in assigned:
                assigned[name] = (5 if variant == 0 else 2) + len(assigned) % max(1, steps + 1)
                assigned[name] %= steps + 1
            value = assigned[name]
            layout_values[name] = value
            rendered.append(f"{REGISTER_PREFIX[base[:1]]}{value}")
        elif IMM_RE.match(base):
            lo, hi = operand["range"] if operand["range"] else (0, 0)
            offset = lo if variant == 0 else hi
            step = (hi - lo) / steps if steps else 1.0
            field = int(round(offset / step)) if step else 0
            layout_values[name] = field % (1 << width)
            rendered.append(str(offset))
        elif NUMERIC_RE.match(base):
            value = min(2 if variant == 0 else 1, steps)
            layout_values[name] = value
            rendered.append(str(value))
        else:
            # A literal operand from the syntax line is emitted as printed: it is fixed by the manual, not
            # synthesised (EE.SRS.ACCX is written `au, as, 0`).
            layout_values[name] = operand.get("literal", 0)
            rendered.append(str(operand.get("literal", 0)))
    return layout_values, rendered


def check_instruction(instruction: dict, *, table: dict | None = None) -> dict:
    """Assemble one instruction from its syntax line and compare with the manual's own diagram."""
    info = toolchain_info()
    if not info["available"]:
        return {"instruction": instruction["name"], "status": "toolchain_missing", **info}
    got = syntax_operands(instruction.get("assembler_syntax") or "")
    name = instruction["name"]
    result: dict = {
        "instruction": name,
        "citation": {"page": instruction.get("instruction_word_page"),
                     "what": f"1.8 {name} instruction word"},
    }
    if not got["mnemonic"]:
        return {**result, "status": "no_syntax"}
    if got["mnemonic"].upper() != name.upper():
        # The syntax line names a different instruction -- a manual typo, not an encoding question.
        return {**result, "status": "syntax_names_other_instruction",
                "syntax_mnemonic": got["mnemonic"], "syntax_page": instruction.get("assembler_syntax_page")}

    fields, unsupported = parse_layout((instruction.get("instruction_word") or "").split("\n"))
    if unsupported:
        return {**result, "status": "layout_not_machine_readable", "unsupported_tokens": unsupported,
                "note": "the extracted instruction-word diagram is not a pure field list, so no encoding "
                        "can be derived from it; fix the extraction for this instruction"}
    if not fields:
        return {**result, "status": "layout_empty"}

    widths = operand_widths(fields)
    matched, unmatched = match_operands(got, fields)
    if unmatched:
        return {**result, "status": "layout_operand_not_in_syntax",
                "diagram_operands": sorted(widths), "unmatched": unmatched}
    comparisons = []
    for variant in (0, 1):
        operands, rendered = synthesise(matched, widths, variant)
        derived = encode_from_manual(fields, operands)
        asm = assemble(f"{name} {', '.join(rendered)}")
        if not asm["accepted"]:
            return {**result, "status": "assembler_rejected", "assembly": f"{name} {', '.join(rendered)}",
                    "stderr": asm.get("stderr"), "errors": asm.get("errors")}
        # The wrapper's entry/retw.n are excluded; exactly one encoded instruction must remain.
        body = asm["body_instructions"]
        if len(body) != 1:
            return {**result, "status": "unexpected_instruction_count",
                    "assembly": f"{name} {', '.join(rendered)}",
                    "encoded": [i["source"] for i in body],
                    "note": "the assembler emitted more or fewer than one instruction for this line"}
        actual = body[0]["word"]
        manual_bits = derived["bits"]
        actual_bits = format(int(actual, 16), f"0{len(actual) * 4}b")
        width_agrees = len(manual_bits) == len(actual_bits)
        differences = []
        if width_agrees:
            differences = [len(manual_bits) - 1 - i for i, (x, y) in enumerate(zip(manual_bits, actual_bits))
                           if x != y]
        comparisons.append({
            "assembly": f"{name} {', '.join(rendered)}",
            "operands": operands,
            "manual_word": derived["word"], "toolchain_word": actual,
            "manual_bits": manual_bits, "toolchain_bits": actual_bits,
            "bit_width_manual": len(manual_bits), "bit_width_toolchain": len(actual_bits),
            "differing_bit_indexes_from_lsb": differences[:16],
            "match": width_agrees and not differences,
            "encoded_mnemonic": body[0]["source"],
            "field_detail": derived["fields_used"],
            "note": None if width_agrees else
                    f"the diagram's fields add up to {len(manual_bits)} bits; this instruction is "
                    f"{len(actual_bits)} bits wide, so the extracted diagram is missing or duplicating "
                    f"part of a field",
        })
    all_match = all(c["match"] for c in comparisons)
    return {**result, "status": "match" if all_match else "mismatch", "comparisons": comparisons}


def check_all(*, limit: int = 0, instructions_path: str = INSTRUCTIONS) -> dict:
    table = json.load(open(instructions_path, encoding="utf-8"))
    info = toolchain_info()
    if not info["available"]:
        return {"status": "toolchain_missing", **info}
    items = table[:limit] if limit else table
    by_status: dict[str, list[str]] = {}
    details = []
    for instruction in items:
        r = check_instruction(instruction)
        by_status.setdefault(r["status"], []).append(instruction["name"])
        details.append(r)
    total = len(items)
    matched = len(by_status.get("match", []))
    return {
        "toolchain": {k: info[k] for k in ("assembler", "version")},
        "total": total, "matched": matched,
        "match_rate": round(matched / total, 4) if total else None,
        "by_status": {k: {"count": len(v), "instructions": v[:40]} for k, v in sorted(by_status.items())},
        "details": details,
        "note": "match means: substituting the operands into the manual's own instruction-word diagram "
                "produces exactly the word the Espressif assembler emits, for two operand sets.",
    }


def errata(*, instructions_path: str = INSTRUCTIONS) -> dict:
    """Every instruction whose extracted diagram and the toolchain disagree, with the evidence.

    Only the disagreements are kept: the 200-odd agreements are asserted by the self-test, and a file that
    lists them would be regenerated noise. This is what the MCP reports as the manual's known defects.
    """
    result = check_all(instructions_path=instructions_path)
    if result.get("status") == "toolchain_missing":
        return result
    keep = ("instruction", "status", "citation", "assembly", "errors", "unsupported_tokens", "note",
            "syntax_mnemonic", "syntax_page", "manual_instruction_word")
    items = []
    for d in result["details"]:
        if d["status"] == "match":
            continue
        entry = {k: d[k] for k in keep if k in d and d[k] is not None}
        if d.get("comparisons"):
            entry["comparisons"] = [{k: c[k] for k in
                                     ("assembly", "manual_word", "toolchain_word", "bit_width_manual",
                                      "bit_width_toolchain", "differing_bit_indexes_from_lsb", "note")
                                     if c.get(k) is not None} for c in d["comparisons"]]
        items.append(entry)
    return {
        "provenance": {
            "kind": "manual_vs_toolchain",
            "note": "Each entry is an instruction whose extracted instruction-word diagram does not "
                    "reproduce what the Espressif assembler emits. An entry means the two disagree -- "
                    "usually the manual -- never that this repository has decided which is right.",
            "toolchain": result["toolchain"],
            "generated_by": "tools/asm_toolchain.py --errata",
            "scope": "every PIE instruction in data/pie_instructions.json that can be expressed as "
                     "assembly, checked with two operand sets",
        },
        "summary": {"checked": result["total"], "matched": result["matched"],
                    "disagreeing": result["total"] - result["matched"],
                    "by_status": {k: v["count"] for k, v in result["by_status"].items() if k != "match"}},
        "instructions": items,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snippet", help="assemble a snippet (function body) and print its encodings")
    ap.add_argument("--raw", action="store_true", help="with --snippet: assemble the snippet verbatim")
    ap.add_argument("--decode", help="disassemble one instruction word (MSB-first hex, e.g. cd2034)")
    ap.add_argument("--check-asm", help="assemble one line, e.g. 'ld.qr q0, a3, 0'")
    ap.add_argument("--check-instruction", help="compare the manual's diagram with the toolchain")
    ap.add_argument("--check-all", action="store_true", help="do that for every extracted instruction")
    ap.add_argument("--errata", metavar="PATH", help="write the disagreements to this JSON file")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", help="write the full result of --check-all to this file")
    a = ap.parse_args()

    if a.snippet:
        print(json.dumps(assemble(a.snippet, raw=a.raw), indent=1, ensure_ascii=False))
        return 0
    if a.decode:
        print(json.dumps(decode(a.decode), indent=1, ensure_ascii=False))
        return 0
    if a.check_asm:
        print(json.dumps(assemble(a.check_asm), indent=1, ensure_ascii=False))
        return 0
    if a.check_instruction:
        table = {i["name"]: i for i in json.load(open(INSTRUCTIONS, encoding="utf-8"))}
        key = a.check_instruction.upper()
        if key not in table:
            print(f"unknown instruction {a.check_instruction}; known: {len(table)}", file=sys.stderr)
            return 2
        print(json.dumps(check_instruction(table[key]), indent=1, ensure_ascii=False))
        return 0
    if a.check_all:
        result = check_all(limit=a.limit)
        if a.json:
            json.dump(result, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        summary = {k: v for k, v in result.items() if k != "details"}
        print(json.dumps(summary, indent=1, ensure_ascii=False))
        return 0 if result.get("matched") == result.get("total") else 1
    if a.errata:
        result = errata()
        if result.get("status") == "toolchain_missing":
            print(json.dumps(result, indent=1), file=sys.stderr)
            return 3
        json.dump(result, open(a.errata, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(json.dumps(result["summary"], indent=1, ensure_ascii=False))
        for item in result["instructions"]:
            print(f"  {item['status']:32s} {item['instruction']}")
        print(f"\n-> {a.errata}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
