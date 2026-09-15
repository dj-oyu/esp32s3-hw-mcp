#!/usr/bin/env python3
"""Apply the fix tools/check_abi.py asks for: spill and reload a10..a15 in the kernels that write them.

Mechanical by design -- the analysis (which register, which line, which frame) comes from check_abi.py, so the
fix cannot drift from the check. Per function that writes any of a10..a15:

  * the `entry a1, N` frame grows, if needed, so the new slots fit (N stays a multiple of 16);
  * one `s32i <reg>, a1, <off>` per register goes in the prologue, at offsets just past the old frame;
  * one `l32i <reg>, a1, <off>` group goes in front of EVERY exit (`retw.n`) of that function.

An exit reached by a jump from elsewhere in the function (ex21's shared epilogue) gets its reload group too,
because the reloads are inserted in front of the `retw.n` line itself.

Functions that a `.macro` generates are fixed once, in the macro body: the same registers and the same line
numbers come back for every instantiation, so the edits are deduped on (file, entry line, exit lines, regs).

    .venv/bin/python tools/fix_abi.py            # all examples/firmware/main/*.S + proposed/*.S
    .venv/bin/python tools/fix_abi.py --dry-run  # print the edits, change nothing
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location("check_abi", os.path.join(HERE, "check_abi.py"))
cabi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cabi)

ENTRY_RE = re.compile(r"^(\s*entry\s+(?:a1|sp),\s*)(\d+)(.*)$")


def fix_file(path: str, pie, dry: bool) -> list[str]:
    raw = open(path, encoding="utf-8").read().splitlines()
    lines, _ = cabi.expand_macros(cabi.strip_comments(raw))
    funcs = cabi.find_functions(lines, os.path.relpath(path, ROOT))
    report: list[str] = []

    sites: dict[tuple[int, tuple[int, ...], tuple[str, ...]], set[str]] = {}
    names: dict[tuple[int, tuple[int, ...]], set[str]] = {}
    for f in funcs:
        u = cabi.analyse(f, pie)
        regs = sorted({w[1] for w in u.writes if cabi.SAVED_LOW <= int(w[1][1:]) <= cabi.SAVED_HIGH},
                      key=lambda r: int(r[1:]))
        if not regs or f.frame is None or not f.exits:
            continue
        key = (f.entries[0], tuple(sorted(ln for ln, _ in f.exits)), tuple(regs))
        sites.setdefault(key, set()).update(regs)
        names.setdefault((f.entries[0], tuple(sorted(ln for ln, _ in f.exits))), set()).add(f.name)

    # apply from the bottom of the file up so earlier line numbers stay valid
    for key in sorted(sites, key=lambda k: k[0], reverse=True):
        entry_ln, exit_lns, _ = key
        regs = sorted(sites[key], key=lambda r: int(r[1:]))
        n = len(regs)
        m = ENTRY_RE.match(raw[entry_ln - 1])
        if not m:
            report.append(f"  SKIP  {path}:{entry_ln} entry line not parseable: {raw[entry_ln - 1]!r}")
            continue
        old = int(m.group(2))
        slots = [old + 4 * i for i in range(n)]
        # The frame grows at its bottom: the frame top (a1 + N == the caller's sp) has to stay where it is,
        # because the call8 window-overflow handler spills the ancestor frame's a0..a3 into [a1+N-16, a1+N).
        # So the new slots go just past the old frame and one extra 16-byte block keeps that top area free
        # for the handler -- without it the reloads read back whatever the handler left there (check_abi.py
        # rule 5; measured on the device 2026-09-15).
        new = old + 4 * n + 16
        if new % 16:
            new += 16 - (new % 16)
        indent = re.match(r"^(\s*)", raw[entry_ln - 1]).group(1)
        label = "/".join(sorted(names[(entry_ln, exit_lns)]))
        pairs = ", ".join(f"{r} ({cabi.caller_name(r)})" for r in regs)

        prologue = [f"{indent}/* ABI: this kernel writes {', '.join(regs)} -- the call8 window makes the",
                    f"{indent} * callee's a10..a15 the caller's a2..a7, so they are saved here and restored",
                    f"{indent} * before every retw.n (tools/check_abi.py). */"]
        prologue += [f"{indent}s32i {r}, a1, {off}" for r, off in zip(regs, slots)]

        for ln in sorted(exit_lns, reverse=True):
            group = [f"{indent}/* ABI: put the caller's {'/'.join(cabi.caller_name(r).split()[-1] for r in regs)}"
                     f" back before returning. */"]
            group += [f"{indent}l32i {r}, a1, {off}" for r, off in zip(regs, slots)]
            raw[ln - 1:ln - 1] = group
        raw[entry_ln - 1] = f"{m.group(1)}{new}{m.group(3)}"
        raw[entry_ln:entry_ln] = prologue
        report.append(f"  fix   {os.path.relpath(path, ROOT)}:{entry_ln} {label}: entry {old} -> {new}, "
                      f"slots {slots}, {pairs}, {len(exit_lns)} exit(s)")

    if not dry and report:
        open(path, "w", encoding="utf-8").write("\n".join(raw) + "\n")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="spill/reload a10..a15 in the kernels (see check_abi.py)")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.files:
        files = a.files
    else:
        proposed = os.path.join(cabi.MAIN_DIR, "proposed")
        files = (sorted(os.path.join(cabi.MAIN_DIR, f) for f in os.listdir(cabi.MAIN_DIR) if f.endswith(".S")) +
                 sorted(os.path.join(proposed, f) for f in os.listdir(proposed) if f.endswith(".S")))
    pie = cabi.pie_write_positions()
    total = 0
    for path in files:
        rep = fix_file(path, pie, a.dry_run)
        total += len(rep)
        for line in rep:
            print(line)
    print(f"{'would fix' if a.dry_run else 'fixed'} {total} function(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
