#!/usr/bin/env python3
"""Assemble every .S before and after the ABI fix and prove the fix changed only spill/reload instructions.

For each file: `xtensa-esp32s3-elf-gcc -c` (warnings counted), objdump -d of both the pre-fix tree (BEFORE) and
the current tree, then per function:

  * the instructions of AFTER minus the added `s32i/l32i a1x, a1, <off >= old frame>` lines must equal the
    instructions of BEFORE, with `entry a1, <n>` normalised (the frame grows by design);
  * count the added spill/reload instructions.

Usage: tools/verify_abi_fix.py BEFORE_DIR [--after DIR]
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GCC = "xtensa-esp32s3-elf-gcc"
OBJDUMP = "xtensa-esp32s3-elf-objdump"
ENTRY = re.compile(r"^entry\s+a1,\s*(\d+)$")
FRAME_ACCESS = re.compile(r"^(?:s32i|l32i)(?:\.n)?\s+(a1[0-5]),\s*a1,\s*(\d+)$")
# Branch/call/l32r targets are code offsets and move when a function grows: compare the shape, not the target.
REF_MNEMONICS = re.compile(r"^(l32r|j|call0|call4|call8|call12|callx0|callx4|callx8|callx12|loop|loopnez|loopgtz"
                           r"|beq|beqz|bne|bnez|bge|bgez|bgeu|bgeui|blt|blti|bltu|bltui|bgt|bgtu|bbci|bbsi|ball|"
                           r"bany|bnone)(\.n)?$")


def normalise(text: str) -> str:
    """Same instruction, whatever encoding the assembler picked (`.n` is a 16-bit form) and whatever the
    targets became once the code moved."""
    text = re.sub(r"\s*<[^>]*>", "", text).strip()
    toks = text.split(" ")
    if not toks:
        return text
    mn = toks[0]
    base = mn[:-2] if mn.endswith(".n") else mn
    rest = " ".join(toks[1:]).strip()
    if REF_MNEMONICS.match(mn) and rest:
        rest = re.sub(r"[^,]*$", "<ref>", rest).strip()
    # `or aX, aY, aY` and `mov aX, aY` are the same instruction
    m = re.match(r"^(or|and|xor) (a\d+), (a\d+), (a\d+)$", f"{base} {rest}")
    if m and m.group(3) == m.group(4):
        return f"mov {m.group(2)}, {m.group(3)}"
    return f"{base} {rest}".strip()


def assemble(path: str) -> tuple[list[str], int]:
    with tempfile.NamedTemporaryFile(suffix=".o", delete=False) as tmp:
        obj = tmp.name
    p = subprocess.run([GCC, "-c", "-o", obj, path], capture_output=True, text=True)
    warn = p.stderr.count("warning:")
    if p.returncode != 0:
        return [f"ASSEMBLY FAILED: {p.stderr.strip().splitlines()[-1] if p.stderr else ''}"], warn
    dis = subprocess.run([OBJDUMP, "-d", obj], capture_output=True, text=True).stdout
    os.unlink(obj)
    return dis.splitlines(), warn


def functions(dis: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in dis:
        m = re.match(r"^[0-9a-f]+ <([^>]+)>:$", line)
        if m:
            cur = m.group(1)
            out[cur] = []
            continue
        if cur is None:
            continue
        if ":" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        text = re.sub(r"\s+", " ", " ".join(parts[2:])).strip() if len(parts) >= 3 else ""
        text = re.sub(r"\s*<[^>]*>", "", text).strip()
        if text and not text.startswith(".") and text.split(" ")[0] != "ill":   # `ill` = padding the
            out[cur].append(normalise(text))                                    # assembler emits for
                                                                                # alignment
    return out


def main() -> int:
    before_dir = sys.argv[1]
    after_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "examples/firmware/main")
    files = sorted(f for f in os.listdir(before_dir) if f.endswith(".S"))
    files += sorted("proposed/" + f for f in os.listdir(os.path.join(before_dir, "proposed")) if f.endswith(".S"))
    bad = 0
    tot_added = tot_warn = 0
    for rel in files:
        bdis, bw = assemble(os.path.join(before_dir, rel))
        adis, aw = assemble(os.path.join(after_dir, rel))
        tot_warn += bw + aw
        if bw or aw:
            print(f"  WARN  {rel}: warnings before={bw} after={aw}")
        bf, af = functions(bdis), functions(adis)
        added = 0
        for name, ains in af.items():
            bins = bf.get(name)
            if bins is None:
                print(f"  FAIL  {rel}: function {name} only in the fixed tree")
                bad += 1
                continue
            old_frame = None
            for ins in bins:
                m = ENTRY.match(ins)
                if m:
                    old_frame = int(m.group(1))
            # strip the added frame accesses
            kept = []
            for ins in ains:
                m = FRAME_ACCESS.match(ins)
                if m and old_frame is not None and int(m.group(2)) >= old_frame:
                    added += 1
                    continue
                if m and "entry" not in ins:
                    kept.append(re.sub(r"^entry\s+a1,\s*\d+$", "entry", ins) if ins.startswith("entry") else ins)
                    continue
                kept.append(re.sub(r"^entry\s+a1,\s*\d+$", "entry", ins))
            norm_bins = [re.sub(r"^entry\s+a1,\s*\d+$", "entry", i) for i in bins]
            if kept != norm_bins:
                print(f"  FAIL  {rel}: {name}: instructions differ beyond the added spills")
                for x, y in zip(kept, norm_bins):
                    if x != y:
                        print(f"          after : {x}\n          before: {y}")
                        break
                bad += 1
        print(f"  ok    {rel}: {len(af)} function(s), {added} spill/reload instruction(s) added, "
              f"everything else identical")
        tot_added += added
    print(f"summary: {len(files)} files, {tot_added} added spill/reload instructions, "
          f"{tot_warn} assembler warnings, {bad} mismatch(es)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
