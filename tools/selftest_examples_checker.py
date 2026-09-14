#!/usr/bin/env python3
"""Self-test for tools/check_examples_log.py.

The checker is the thing that decides whether a run from the device passed, so it has to be tested without a
device: this builds a log that *should* pass from the reference implementations, then mutates one field at a
time and insists that the checker notices. A check that survives every mutation is not checking anything.

    .venv/bin/python tools/selftest_examples_checker.py
"""
from __future__ import annotations

import importlib.util
import io
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("cel", os.path.join(HERE, "check_examples_log.py"))
cel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cel)


def hex16(values, width=4) -> str:
    return ",".join(f"{(v & ((1 << (4 * width)) - 1)):0{width}x}" for v in values)


def build_log() -> str:
    """A log that must pass: every value is a reference value, in the firmware's exact format."""
    rng = random.Random(0x1234)
    a = [rng.randint(-100, 100) for _ in range(256)]
    b = [rng.randint(-100, 100) for _ in range(256)]
    bt = [b[j * 16 + i] for i in range(16) for j in range(16)]
    c = cel.ref_matmul(a, bt)
    x = [rng.randint(-3000, 3000) for _ in range(64)]
    h = [1000 - i * 90 for i in range(16)]
    y_raw = cel.ref_fir(x, h, 49, 0)
    y_q15 = cel.ref_fir(x, h, 49, 15)
    acc, maths = cel.ref_accx(200, 32000)

    lines = ["ENV chip=esp32s3 cores=2", "EX ex01 encoding BEGIN"]
    steps = [("ld_accx+0", 0, 0), ("ld_accx+16", 16, 16), ("ld_accx+32", 32, 32), ("ld_accx-16", -16, -16),
             ("st_accx+0", 0, 0), ("st_accx+16", 16, 16), ("st_accx+32", 32, 32), ("ld128+0", 0, 0),
             ("ld128+16", 16, 16), ("ld128+32", 32, 32), ("vld128+0", 0, 0), ("vld128+16", 16, 16),
             ("vst128+16", 16, 16)]
    for label, got, printed in steps:
        lines.append(f"EX ex01 encoding DATA step_{label}={got}")
    lines += [
        "EX ex01 encoding DATA step_summary ok=13 mismatch=0",
        "EX ex01 encoding DATA raw_field1_ld=8",
        "EX ex01 encoding DATA raw_field1_st=8",
        "EX ex01 encoding CHECK raw_ld_field1_follows_pseudocode pie=8 ref=8 ok",
        "EX ex01 encoding CHECK raw_st_field1_follows_assembler pie=8 ref=8 ok",
    ]
    for value, shift in cel.EX01_SRS:
        want = cel.sat32(value >> shift)
        lines.append(f"EX ex01 encoding CHECK srs_accx pie={want} ref={want} ok")
    lines += ["EX ex01 encoding DATA bitrev_3fc90000=00000000,00000000,00000000,00000000",
              "EX ex01 encoding RESULT ok=15 fail=0",
              "EX ex02 matmul16 BEGIN",
              f"EX ex02 matmul16 DATA a={hex16(a)}",
              f"EX ex02 matmul16 DATA bt={hex16(bt)}",
              f"EX ex02 matmul16 DATA c={hex16(c, 8)}",
              "EX ex02 matmul16 DATA cycles_us=120",
              "EX ex02 matmul16 CHECK elements_matching_reference pie=256 ref=256 ok",
              "EX ex02 matmul16 RESULT ok=1 fail=0",
              "EX ex03 fir16 BEGIN",
              f"EX ex03 fir16 DATA y_raw={hex16(y_raw, 8)}",
              f"EX ex03 fir16 DATA shift=0 matched=49 of 49",
              f"EX ex03 fir16 DATA y_q15={hex16(y_q15, 8)}",
              f"EX ex03 fir16 DATA shift=15 matched=49 of 49",
              f"EX ex03 fir16 DATA x={hex16(x)}",
              f"EX ex03 fir16 DATA h={hex16(h)}",
              "EX ex03 fir16 CHECK samples_matching_reference pie=98 ref=98 ok",
              "EX ex03 fir16 RESULT ok=1 fail=0",
              "EX ex04 qr BEGIN",
              "EX ex04 qr CHECK qr_copy_256_bytes pie=1 ref=1 ok",
              "EX ex04 qr CHECK qr_move_roundtrip_16_bytes pie=1 ref=1 ok",
              "EX ex04 qr DATA interlock_d0 stall=1.000 dep=20000 ind=18000 iters=2000",
              "EX ex04 qr DATA interlock_d1 stall=0.000 dep=18000 ind=18000 iters=2000",
              "EX ex04 qr DATA interlock_d2 stall=0.000 dep=18000 ind=18000 iters=2000",
              "EX ex04 qr CHECK d0 pie=1 ref=1 ok",
              "EX ex04 qr CHECK d1 pie=0 ref=0 ok",
              "EX ex04 qr CHECK d2 pie=0 ref=0 ok",
              "EX ex04 qr RESULT ok=5 fail=0",
              "EX ex05 saturation BEGIN",
              f"EX ex05 saturation DATA mathematical_sum={maths}",
              f"EX ex05 saturation DATA clamped_40bit={acc}",
              f"EX ex05 saturation DATA per_iteration={8 * 32000 * 32000}",
              f"EX ex05 saturation DATA readout_sat32={cel.sat32(acc)}",
              f"EX ex05 saturation DATA readout_shifted9={acc >> 9}",
              "EX ex05 saturation RESULT ok=3 fail=0",
              "EX ex06 fft BEGIN",
              f"EX ex06 fft DATA r2bf_sel0_out={hex16(cel.ref_r2bf(cel.EX06_X, 0))}",
              f"EX ex06 fft DATA r2bf_sel1_out={hex16(cel.ref_r2bf(cel.EX06_X, 1))}",
              f"EX ex06 fft DATA cmul_half0_sar0={hex16(cel.ref_cmul(cel.EX06_U, cel.EX06_V, 0, 0)[:4])}",
              f"EX ex06 fft DATA cmul_half1_sar0={hex16(cel.ref_cmul(cel.EX06_U, cel.EX06_V, 0, 1)[4:])}",
              f"EX ex06 fft DATA cmul_half0_sar12={hex16(cel.ref_cmul(cel.EX06_U, cel.EX06_V, 12, 0)[:4])}",
              f"EX ex06 fft DATA cmul_half1_sar12={hex16(cel.ref_cmul(cel.EX06_U, cel.EX06_V, 12, 1)[4:])}",
              "EX ex06 fft RESULT ok=10 fail=0",
              "SUMMARY checks_ok=35 checks_fail=0", "END", ""]
    return "\n".join(lines)


def run_checker(text: str) -> tuple[int, str]:
    path = tempfile.mktemp(suffix=".log")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    old_argv, old_stdout = sys.argv, sys.stdout
    sys.argv = ["check_examples_log.py", path]
    buf = io.StringIO()
    sys.stdout = buf
    try:
        rc = cel.main()
    finally:
        sys.argv, sys.stdout = old_argv, old_stdout
        os.unlink(path)
    return rc, buf.getvalue()


# Each mutation names the DATA key to corrupt; the checker must report a failure.
MUTATIONS = [
    ("ex02 c", "EX ex02 matmul16 DATA c=", "00000001"),
    ("ex02 a", "EX ex02 matmul16 DATA a=", "0000"),
    ("ex03 y_raw", "EX ex03 fir16 DATA y_raw=", "00000001"),
    ("ex05 readout", "EX ex05 saturation DATA readout_shifted9=", "0"),
    ("ex06 r2bf_sel0", "EX ex06 fft DATA r2bf_sel0_out=", "0000,0000"),
    ("ex06 cmul_half1", "EX ex06 fft DATA cmul_half1_sar0=", "0000,0000,0000,0000"),
    ("ex04 interlock", "EX ex04 qr DATA interlock_d0 ", "stall=0.000"),
    ("ex01 raw", "EX ex01 encoding DATA raw_field1_ld=", "3"),
]

failures = []

good = build_log()
rc, out = run_checker(good)
if rc != 0:
    failures.append(f"a log built entirely from the references did not pass:\n{out}")
else:
    print(f"ok   the reference-built log passes ({out.strip().splitlines()[-1]})")

for name, needle, replacement in MUTATIONS:
    lines = good.splitlines()
    hits = 0
    for i, line in enumerate(lines):
        if line.startswith(needle):
            head = needle if needle.endswith(" ") else needle
            rest = line[len(head):]
            if needle.endswith(" "):
                lines[i] = head + replacement
            else:
                lines[i] = head + replacement
            hits += 1
    if hits != 1:
        failures.append(f"{name}: expected exactly one line starting with {needle!r}, found {hits}")
        continue
    rc, out = run_checker("\n".join(lines))
    caught = rc != 0 and "FAIL" in out
    print(f"{'ok  ' if caught else 'FAIL'} mutating {name} is caught")
    if not caught:
        failures.append(f"{name}: the checker still passed after corrupting it")

# A log with no EX lines must be rejected loudly, not silently pass.
rc, out = run_checker("hello\n")
if rc == 2:
    print("ok   a log without EX lines is rejected (exit 2)")
else:
    failures.append(f"a log without EX lines returned {rc}, expected 2")

print()
if failures:
    print(f"FAILED: {len(failures)} problem(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASSED: the checker passes a reference log and catches every mutation")
