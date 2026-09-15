#!/usr/bin/env python3
"""Lint the hand-written PIE kernels for the call8 callee rule (a10..a15 are the caller's a2..a7).

THE RULE, AND WHY IT EXISTS
---------------------------
`xtensa-esp32s3-elf-gcc -Os` keeps every value that is live across a `call8` in **a2..a7** (measured, not
assumed: a function with a pointer and a limit across a `call8` compiles to `mov.n a6, a2 / mov.n a7, a3 /
... call8 cl`, i.e. the pointer stays in a6 and the limit in a7 across the call). The register window then
makes the callee's names overlap the caller's:

    callee aN  ==  caller a(N+8) mod 16        (call8 increments the window by 8 registers)

so, concretely:

    callee a2 .. a7   == caller a10..a15   the argument registers: the caller writes them, the callee reads
    callee a10..a15   == caller a2 ..a7    WHAT THIS LINT IS ABOUT
    callee a8         == caller a0         the link slot call8 itself writes  -> caller-saved
    callee a9         == caller a1         the slot the callee's own a1 aliases -> caller-saved

Therefore a hand-written callee that writes a10..a15 without saving them destroys whichever of the caller's
a2..a7 gcc had live. A leaf may write a8/a9 freely: those are the registers the compiler itself spills
around its own calls (`s32i.n a9, sp, 0` before `call8`, `l32i.n a9, sp, 0` after), which is exactly what
"caller-saved" means on this target.

Device evidence for the rule (the reason this tool exists): `ex12_dist2_qacc` used a15 as its group counter.
At loop exit a15 == 0, which is the caller's a7 -- the array pointer main.c held across the call -- so the
next `l16ui a12, a7, 0` in print_i16 took a LoadProhibited with EXCVADDR = 0 and A7 = 0.

CHECKS
------
  1. writes     : every write to a10..a15 inside a function must be bracketed by a spill to, and a reload
                  from, the same slot of that function's own stack frame. Which operand of a PIE instruction
                  is written is read out of data/pie_instructions.json -- the pseudo-code's assignment
                  target (`as[31:0] = as[31:0] + imm16`) rather than a hand-written guess -- so the
                  `.IP`/`.INCP` forms that post-increment their address register are covered by construction.
  2. entry/retw : a function entered by `call8` must leave the window with `retw.n`; a bare `ret` (call0 ABI,
                  no window restore) and more than one `entry` per function are failures.
  3. loopgtz    : the body of a zero-overhead loop (loop / loopnez / loopgtz) must fit the instruction's
                  offset field (256 bytes, the limit ex17_mp3synth.md quotes); the body is measured at a
                  worst-case 4 bytes per instruction, so a flag is never a false negative.
  4. frame      : a stack access outside the frame `entry` allocated, or a frame size that is not a multiple
                  of 16, is a warning.

Usage:
    .venv/bin/python tools/check_abi.py                 # examples/firmware/main/*.S + proposed/*.S
    .venv/bin/python tools/check_abi.py --strict        # nonzero exit if anything is flagged
    .venv/bin/python tools/check_abi.py a.S b.S
Output is one `file:line function: what is wrong` line per finding, then counts; a clean tree prints
`0 violations`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
MAIN_DIR = os.path.join(ROOT, "examples", "firmware", "main")

SAVED_LOW, SAVED_HIGH = 10, 15     # callee a10..a15 == caller a2..a7: must be preserved
LOOP_LIMIT = 256                   # bytes, the zero-overhead-loop offset field
WORST_INSN_BYTES = 4               # conservative: narrow instructions are 2 or 3 bytes

# The PIE pseudo-code names an address register as/au/ad/ax/ay/at; an operand is written when the operation
# text assigns to that name. This is how the write set below is derived from the manual, not guessed.
AR_NAMES = ("as", "au", "ad", "ax", "ay", "at")

# Base Xtensa (non-PIE) mnemonics: the first operand is the destination AR.
BASE_W0 = {
    "add", "add.n", "addi", "addi.n", "addx2", "addx4", "addx8", "abs", "and", "extui", "l16si", "l16ui",
    "l32i", "l32i.n", "l32r", "l8ui", "max", "maxu", "min", "minu", "mov", "mov.n", "movi", "movi.n",
    "movnez", "mul16s", "mul16u", "mull", "neg", "nsau", "or", "sext", "sll", "slli", "sra", "srai",
    "srl", "srli", "sub", "subx2", "subx4", "subx8", "xor", "clamps",
}
BASE_W0_PREFIX = ("rsr.", "rur.")                 # rsr.ccount a2 / rur.qacc_l_0 a6
BASE_NO_WRITE = {
    "beq", "beqz", "bge", "bgeu", "bgez", "bgt", "bgtu", "bgeui", "blt", "blti", "bltu", "bltui", "bne",
    "bnez", "ball", "bany", "bbc", "bbci", "bbs", "bbsi", "bnone", "break", "call0", "call4", "call8",
    "call12", "callx0", "callx4", "callx8", "callx12", "entry", "esync", "ill", "isync", "j", "loop",
    "loopnez", "loopgtz", "memw", "nop", "nop.n", "ret", "ret.n", "retw", "retw.n", "rsync", "simcall",
    "ssa8l", "ssa8b", "ssai", "ssr", "syscall", "wsr",
}
STORE_PREFIX = ("s32i", "s16i", "s8i", "s32ri", "s32c1i", "st.", "wsr.")
AR_RE = re.compile(r"^a(\d+)$")


class Report:
    """The same shape as tools/verify_pie.py's Report, so the two read alike in CI."""

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


def caller_name(reg: str) -> str:
    """'a15' -> 'the caller's a7': what that register is on the caller's side of the call8."""
    return f"the caller's a{int(reg[1:]) - 8}"


# -------------------------------------------------------------------------------------------------- parsing
def strip_comments(lines: list[str]) -> list[tuple[int, str]]:
    """[(line number, line without /* */ and # comments)]. Line numbers are preserved so every finding points
    at a source line a human can open."""
    out: list[tuple[int, str]] = []
    in_block = False
    for n, raw in enumerate(lines, 1):
        line = raw
        if in_block:
            end = line.find("*/")
            if end == -1:
                out.append((n, ""))
                continue
            line = line[end + 2:]
            in_block = False
        while True:
            start = line.find("/*")
            if start == -1:
                break
            end = line.find("*/", start + 2)
            if end == -1:
                line = line[:start]
                in_block = True
                break
            line = line[:start] + line[end + 2:]
        out.append((n, line.split("#")[0].rstrip()))
    return out


MACRO_DEF_RE = re.compile(r"^\.macro\s+([A-Za-z_][\w.]*)\s*[, \t]\s*(.*)$")


def split_args(s: str) -> list[str]:
    """Split a macro invocation's arguments on commas, but not inside a quoted string (ex04's HAZ takes a
    whole instruction as its third argument and that string contains commas)."""
    out: list[str] = []
    cur = ""
    quoted = False
    for ch in s:
        if ch == '"':
            quoted = not quoted
            cur += ch
        elif ch == "," and not quoted:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip() or out:
        out.append(cur.strip())
    return out


def expand_macros(lines: list[tuple[int, str]]) -> tuple[list[tuple[int, str]], list[str]]:
    """Instantiate the assembler's own `.macro` and `.rept` bodies; returns (expanded lines, notes).

    The examples define kernels through `.macro` (ex01's STEP_AS/STEP_QU, ex04's HAZ, ex09's MAC1/MAC4/CHAIN),
    so a scanner that reads only the literal source text would miss every function they generate and — worse —
    every instruction inside a `.rept` block (ex04's fillers are `add a14, a13, a12`, i.e. a write to the
    caller's a6 only when the filler count is non-zero). Expansion is text substitution of `\\param`; an
    expanded line keeps the *macro body's* line number, so a finding inside a macro points at the definition
    that has to be fixed."""
    notes: list[str] = []
    bodies: dict[str, tuple[list[str], list[tuple[int, str]]]] = {}
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        ln, text = lines[i]
        m = MACRO_DEF_RE.match(text.strip())
        if m:
            name = m.group(1)
            params = [p.strip() for p in (m.group(2) or "").split(",") if p.strip()]
            body: list[tuple[int, str]] = []
            i += 1
            while i < len(lines) and lines[i][1].strip() != ".endm":
                body.append(lines[i])
                i += 1
            bodies[name] = (params, body)
            i += 1
            continue
        out.append((ln, text))
        i += 1

    def expand(lines: list[tuple[int, str]], depth: int = 0) -> list[tuple[int, str]]:
        if depth > 8:
            return lines
        res: list[tuple[int, str]] = []
        i = 0
        while i < len(lines):
            line_no, text = lines[i]
            stripped = text.strip()
            toks = stripped.split(None, 1)
            if toks and toks[0] == ".rept":
                count = toks[1].strip() if len(toks) > 1 else ""
                inner: list[tuple[int, str]] = []
                i += 1
                while i < len(lines) and lines[i][1].strip() != ".endr":
                    inner.append(lines[i])
                    i += 1
                i += 1
                if not count.isdigit():
                    notes.append(f"{line_no}: .rept count {count!r} is not a literal: assumed 1")
                    reps = 1
                else:
                    reps = int(count)
                res.extend(expand(inner, depth + 1) * reps)
                continue
            if toks and toks[0] in bodies:
                params, body = bodies[toks[0]]
                args = [a.strip().strip('"') for a in split_args(toks[1] if len(toks) > 1 else "")]
                args = (args + [""] * len(params))[:len(params)]
                sub = dict(zip(params, args))
                swapped: list[tuple[int, str]] = []
                for bln, btext in body:
                    expanded = btext
                    for p, a in sub.items():
                        expanded = re.sub(r"\\" + re.escape(p) + r"\b", a, expanded)
                    swapped.append((bln, expanded))
                res.extend(expand(swapped, depth + 1))
                i += 1
                continue
            res.append((line_no, text))
            i += 1
        return res

    return expand(out), notes


def pie_write_positions() -> dict[str, tuple[list[str], set[int]]]:
    """{instruction name.upper(): (operand tokens of the Assembler Syntax, operand positions that write an AR)}.

    From data/pie_instructions.json: the syntax gives the operand order, the operation's pseudo-code gives the
    assignment target. `EE.VLD.128.IP qu, as, -2048..2032` with `as[31:0] = as[31:0] + ...` -> position 1
    (the second token) is written, so `EE.VLD.128.IP q0, a2, 16` writes a2 as well as q0."""
    path = os.path.join(DATA, "pie_instructions.json")
    if not os.path.exists(path):
        return {}
    table: dict[str, tuple[list[str], set[int]]] = {}
    for entry in json.load(open(path, encoding="utf-8")):
        name = entry.get("name")
        syn = (entry.get("assembler_syntax") or "").split("\n")[0]
        op = entry.get("operation") or ""
        if not name or not syn:
            continue
        head = re.sub(r"^[A-Za-z0-9_.]+\s*", "", syn)
        raw_tokens = [t for t in re.split(r"[,\s]+", head) if t]
        # The PDF's syntax column sometimes splits one operand into a placeholder and its range
        # ("LD.QR qu, as, imm, -128..112"); merge those so the tokens line up with the asm operands.
        tokens: list[str] = []
        for t in raw_tokens:
            if (tokens and re.fullmatch(r"(imm|sel|upd|sar)\d*", tokens[-1])
                    and re.fullmatch(r"-?\d+\.\.-?\d+", t)):
                tokens[-1] = f"{tokens[-1]}{t}"
            else:
                tokens.append(t)
        written: set[int] = set()
        for idx, tok in enumerate(tokens):
            if tok not in AR_NAMES:
                continue
            if re.search(rf"\b{tok}\b\s*(\[[^\]]*\])?\s*=[^=]", op) or re.search(rf"\b{tok}\b\s*\+=", op):
                written.add(idx)
        table[name.upper()] = (tokens, written)
    return table


class Insn:
    """One instruction line and the AR registers it writes."""

    def __init__(self, line_no: int, text: str) -> None:
        self.line_no = line_no
        self.text = text.strip()
        toks = self.text.split(None, 1)
        self.mnemonic = toks[0]
        self.operands = [o.strip() for o in toks[1].split(",")] if len(toks) > 1 else []
        self.unknown = ""            # non-empty when the write set could not be resolved

    def __repr__(self) -> str:
        return f"<{self.line_no}:{self.text}>"


def writes(insn: Insn, pie: dict[str, tuple[list[str], set[int]]]) -> set[str]:
    """The AR registers this instruction writes. Sets insn.unknown when that cannot be decided, so the caller
    reports it instead of silently assuming the instruction is harmless."""
    mn = insn.mnemonic
    key = mn.upper()
    if key in pie:
        tokens, positions = pie[key]
        if len(tokens) != len(insn.operands):
            insn.unknown = (f"{mn}: syntax has {len(tokens)} operands {tokens}, "
                            f"this line has {len(insn.operands)}")
            return set()
        return {insn.operands[i] for i in positions if AR_RE.match(insn.operands[i])}
    low = mn.lower()
    if low in BASE_W0 or low.startswith(BASE_W0_PREFIX):
        return {insn.operands[0]} if insn.operands and AR_RE.match(insn.operands[0]) else set()
    if low in BASE_NO_WRITE or low.startswith(STORE_PREFIX):
        return set()
    if mn.startswith("."):
        if mn == ".byte" and len(insn.operands) == 3:
            # ex01's raw encodings: the manual's diagram ends in `as[3:0]|0100`, so a three-byte form whose
            # low nibble is 0100 carries the address register in bits [7:4].
            try:
                w = [int(o, 0) for o in insn.operands]
            except ValueError:
                insn.unknown = ".byte with non-numeric operands"
                return set()
            word = w[0] | (w[1] << 8) | (w[2] << 16)
            if (word & 0xF) == 0b0100:
                return {f"a{(word >> 4) & 0xF}"}
            insn.unknown = ".byte: not a documented 3-byte PIE form"
        return set()
    insn.unknown = f"unknown mnemonic {mn}"
    return set()


class Func:
    def __init__(self, name: str, file: str, line_no: int) -> None:
        self.name = name
        self.file = file
        self.line_no = line_no
        self.insns: list[Insn] = []
        self.frame: int | None = None
        self.entries: list[int] = []
        self.exits: list[tuple[int, str]] = []
        self.unknown: list[str] = []


FUNC_DECL_RE = re.compile(r"^\s*\.(?:global|type)\s+([A-Za-z_][\w.]*)")


def find_functions(lines: list[tuple[int, str]], path: str) -> list[Func]:
    """A function runs from its declared label to `.size name, .-name` (or to the next declared label, a
    `.section`, or EOF when there is no `.size`). Local labels stay inside the body, because the loop checker
    needs them."""
    decl: set[str] = set()
    for _, text in lines:
        m = FUNC_DECL_RE.match(text)
        if m:
            decl.add(m.group(1))
    funcs: list[Func] = []
    cur: Func | None = None
    for ln, text in lines:
        stripped = text.strip()
        if not stripped:
            continue
        body = stripped
        if body.endswith(":"):
            label = body[:-1].strip()
            if label in decl:
                cur = Func(label, path, ln)
                funcs.append(cur)
                continue
        if cur is not None:
            if re.match(r"^\.size\s+" + re.escape(cur.name) + r"\b", stripped) or stripped.startswith(".section"):
                cur = None
                continue
            cur.insns.append(Insn(ln, stripped))
    return [f for f in funcs if f.insns]


FRAME_PTR_RE = re.compile(r"^(addi|addi\.n|mov|mov\.n)\s+(a\d+),\s*(a\d+)(?:,\s*(-?\d+))?$")
SPILL_RE = re.compile(r"^(s32i|s32i\.n)\s+(a\d+),\s*(a\d+),\s*(-?\d+)$")
RELOAD_RE = re.compile(r"^(l32i|l32i\.n)\s+(a\d+),\s*(a\d+),\s*(-?\d+)$")


class Uses:
    """Per function: the a1-relative frame pointers, the spills/reloads through them, and the AR writes."""

    def __init__(self) -> None:
        self.frame_ptrs: dict[str, int] = {"a1": 0}       # register -> offset from a1
        self.saves: list[tuple[int, str, int]] = []       # (insn index, register, offset from a1)
        self.loads: list[tuple[int, str, int]] = []
        self.writes: list[tuple[int, str, int, str]] = []  # (insn index, register, line, text)
        self.offsets: list[int] = []


def analyse(func: Func, pie) -> Uses:
    u = Uses()
    for idx, insn in enumerate(func.insns):
        text = insn.text
        low = insn.mnemonic.lower()
        if low == "entry":
            if len(insn.operands) >= 2 and insn.operands[1].lstrip("-").isdigit():
                func.frame = int(insn.operands[1])
            func.entries.append(insn.line_no)
            continue
        if low in ("ret", "ret.n", "retw", "retw.n"):
            func.exits.append((insn.line_no, low))
            continue
        m = FRAME_PTR_RE.match(text)
        if m:
            dst, src, imm = m.group(2), m.group(3), m.group(4)
            if src in u.frame_ptrs:
                u.frame_ptrs[dst] = u.frame_ptrs[src] + (int(imm) if imm else 0)
        for reg in sorted(writes(insn, pie), key=lambda x: int(x[1:])):
            u.writes.append((idx, reg, insn.line_no, text))
        m = SPILL_RE.match(text)
        if m and m.group(3) in u.frame_ptrs:
            off = u.frame_ptrs[m.group(3)] + int(m.group(4))
            u.saves.append((idx, m.group(2), off))
            u.offsets.append(off)
        m = RELOAD_RE.match(text)
        if m and m.group(3) in u.frame_ptrs:
            off = u.frame_ptrs[m.group(3)] + int(m.group(4))
            u.loads.append((idx, m.group(2), off))
            u.offsets.append(off)
        if insn.unknown:
            func.unknown.append(f"{insn.line_no}: {insn.unknown}")
    return u


# --------------------------------------------------------------------------------------------------- checks
def shared_exit(f: Func, owners: dict[str, Func]) -> Func | None:
    """ex21_fill_undefined hands off to ex21_fill_undefined_inline with a `j` and shares its epilogue, so a
    function whose body has no exit of its own is allowed to name the function it jumps into instead."""
    for ins in reversed(f.insns):
        if ins.text.startswith(".") or ins.text.endswith(":"):
            continue
        if ins.mnemonic == "j" and ins.operands and ins.operands[0] in owners:
            target = owners[ins.operands[0]]
            return target if target is not f else None
        return None
    return None


def check_func(f: Func, u: Uses, r: Report, owners: dict[str, Func]) -> None:
    # 1. every write to a10..a15 must be bracketed by a spill to, and a reload from, the same frame slot
    for reg in sorted({w[1] for w in u.writes}, key=lambda s: int(s[1:])):
        n = int(reg[1:])
        if not (SAVED_LOW <= n <= SAVED_HIGH):
            continue
        mine = [(i, ln, txt) for i, rr, ln, txt in u.writes if rr == reg]
        slots = {off for i, rr, off in u.saves if rr == reg and off >= 0}
        # a `l32i` that reloads the caller's own value is not a clobber, so it does not count as a write
        reload_idx = {i for i, rr, off in u.loads if rr == reg and off in slots}
        clobbers = [(i, ln, txt) for i, ln, txt in mine if i not in reload_idx]
        if not clobbers:
            continue
        first = clobbers[0][0]
        _, ln0, txt0 = clobbers[0]
        saves = [(i, off) for i, rr, off in u.saves if rr == reg and off >= 0 and i < first]
        if not saves:
            r.bad(f"{f.file}:{ln0} {f.name}: writes {reg} ({caller_name(reg)}) with no spill to the frame "
                  f"(first write: {txt0})")
            continue
        slots = {off for _, off in saves}
        # every exit path must reload the register after the last write that reaches it
        exits = []
        for ln, kind in f.exits:
            idx = next((i for i, ins in enumerate(f.insns) if ins.line_no == ln), None)
            if idx is not None:
                exits.append((idx, ln, kind))
        if not exits:
            r.bad(f"{f.file}:{ln0} {f.name}: writes {reg} ({caller_name(reg)}) but the function has no exit")
            continue
        for exit_idx, ln, kind in exits:
            writes_before = [i for i, _ln, _tx in clobbers if i < exit_idx]
            if not writes_before:
                continue
            last_before = max(writes_before)
            reloads = [(i, off) for i, rr, off in u.loads
                       if rr == reg and off in slots and last_before < i < exit_idx]
            if not reloads:
                r.bad(f"{f.file}:{ln} {f.name}: exit `{kind}` after writing {reg} ({caller_name(reg)}) "
                      f"at instruction {last_before} without reloading it (slots {sorted(slots)})")
    # 2. entry / retw.n pairing
    kinds = {k for _, k in f.exits}
    if "ret" in kinds or "ret.n" in kinds:
        ln = next(ln for ln, k in f.exits if k in ("ret", "ret.n"))
        r.bad(f"{f.file}:{ln} {f.name}: bare `ret` -- a call8 callee must leave the window with retw.n")
    if len(f.entries) > 1:
        r.bad(f"{f.file}:{f.entries[1]} {f.name}: {len(f.entries)} `entry` instructions in one function")
    if f.entries and not kinds:
        shared = shared_exit(f, owners)
        if shared is None:
            r.bad(f"{f.file}:{f.line_no} {f.name}: `entry` but no retw.n exit")
        elif not any(k in ("retw", "retw.n") for _, k in shared.exits):
            r.bad(f"{f.file}:{f.line_no} {f.name}: ends by jumping into {shared.name}, which has no retw.n "
                  f"exit either")
    if kinds and not f.entries:
        r.note(f"{f.file}:{f.line_no} {f.name}: no `entry` (leaf without a frame), retw.n only")
    if f.frame is not None and f.frame % 16:
        r.note(f"{f.file}:{f.line_no} {f.name}: entry frame {f.frame} is not a multiple of 16")
    # 4. frame sanity: a stack access outside the frame entry allocated
    if f.frame is not None:
        for off in u.offsets:
            if off < 0 or off >= f.frame:
                r.note(f"{f.file}:{f.line_no} {f.name}: frame access at offset {off} is outside entry's "
                       f"{f.frame}-byte frame")
    # 5. the top 16 bytes of the frame are the call8 window-spill area. `entry a1, N` keeps the frame top
    #    (a1+N == the caller's sp) where it was, and _WindowOverflow8/_WindowUnderflow8 write the ancestor
    #    frame's a0..a3 into [a1+N-16, a1+N) -- so a local kept there is silently overwritten the first time
    #    the register window overflows (a deep call chain, or any call from inside an ISR). On the device
    #    that shows up much later as a wild pointer, not as a bad store. Measured 2026-09-15: ex12_dist2_qacc
    #    kept its clamp ceiling at offset 52 and its leftover count at 56 in a 64-byte frame, and the examples
    #    firmware panicked with LoadProhibited / EXCVADDR=0 / A2..A7 all zero right after ex12 printed its
    #    distance results. Offsets >= N are the caller's argument area and stay legal.
    if f.frame is not None and f.frame >= 16:
        for off in sorted(set(u.offsets)):
            if f.frame - 16 <= off < f.frame:
                r.bad(f"{f.file}:{f.line_no} {f.name}: frame slot {off} sits in the top 16 bytes of the "
                      f"{f.frame}-byte frame (the call8 window-spill area) -- move it below {f.frame - 16}")
    for msg in f.unknown:
        r.note(f"{f.file} {f.name}: unanalysed instruction -- {msg}")
    # 3. zero-overhead loop bodies
    label_pos = {ins.text.strip()[:-1]: i for i, ins in enumerate(f.insns) if ins.text.strip().endswith(":")}
    for i, ins in enumerate(f.insns):
        if ins.mnemonic.lower() not in ("loop", "loopnez", "loopgtz"):
            continue
        target = ins.operands[-1] if ins.operands else ""
        if target not in label_pos:
            r.note(f"{f.file}:{ins.line_no} {f.name}: {ins.mnemonic} target {target!r} not in this function")
            continue
        body = f.insns[i + 1:label_pos[target] + 1]
        size = WORST_INSN_BYTES * len(body)
        if size > LOOP_LIMIT:
            r.bad(f"{f.file}:{ins.line_no} {f.name}: zero-overhead loop body is >= {size} bytes "
                  f"({len(body)} instructions), over the {LOOP_LIMIT}-byte offset field")


def check_file(path: str, pie, r: Report) -> tuple[int, int, int]:
    raw = open(path, encoding="utf-8").read().splitlines()
    lines, notes = expand_macros(strip_comments(raw))
    rel = os.path.relpath(path, ROOT)
    funcs = find_functions(lines, rel)
    owners = {ins.text.strip()[:-1]: f for f in funcs for ins in f.insns if ins.text.strip().endswith(":")}
    for note in notes:
        r.note(f"{rel} {note}")
    nloops = 0
    analysed = [(f, analyse(f, pie)) for f in funcs]     # analyse every function before any check: the
    for f, u in analysed:                                # shared-exit check reads another function's exits
        check_func(f, u, r, owners)
        nloops += sum(1 for i in f.insns if i.mnemonic.lower() in ("loop", "loopnez", "loopgtz"))
    return len(funcs), sum(len(f.insns) for f in funcs), nloops


def main() -> int:
    ap = argparse.ArgumentParser(description="call8 callee lint for the hand-written PIE kernels")
    ap.add_argument("files", nargs="*", help="default: examples/firmware/main/*.S and proposed/*.S")
    ap.add_argument("--strict", action="store_true", help="also fail on warnings")
    a = ap.parse_args()

    if a.files:
        files = a.files
    else:
        proposed = os.path.join(MAIN_DIR, "proposed")
        files = (sorted(os.path.join(MAIN_DIR, f) for f in os.listdir(MAIN_DIR) if f.endswith(".S")) +
                 sorted(os.path.join(proposed, f) for f in os.listdir(proposed) if f.endswith(".S")))

    pie = pie_write_positions()
    r = Report()
    print("call8 callee rule: the callee's a10..a15 are the caller's a2..a7, where gcc -Os keeps values "
          "live across a call8")
    print("1. writes to a10..a15 without a matching spill/reload, 2. entry/retw.n pairing,")
    print("3. zero-overhead loop bodies <= %d bytes, 4. frame sanity" % LOOP_LIMIT)
    nfunc = ninsn = nloops = 0
    for path in files:
        fn, ni, nl = check_file(path, pie, r)
        nfunc += fn
        ninsn += ni
        nloops += nl
    if r.fail == 0:
        r.ok(f"no write to a10..a15 outside a spill/reload pair ({len(files)} files, {nfunc} functions, "
             f"{ninsn} instructions, {nloops} zero-overhead loops)")
    print(f"summary: {len(files)} files, {nfunc} functions, {ninsn} instructions, "
          f"{r.fail} violations, {r.warn} warnings")
    if r.fail == 0 and r.warn == 0:
        print("0 violations")
    return 1 if (r.fail or (a.strict and r.warn)) else 0


if __name__ == "__main__":
    sys.exit(main())
