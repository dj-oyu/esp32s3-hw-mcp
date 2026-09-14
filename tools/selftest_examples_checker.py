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
              f"EX ex03 fir16 DATA align_probe={hex16(x[:8] + x[:8])}",
              f"EX ex03 fir16 DATA funnel_ab={hex16(x[1:9])}",
              f"EX ex03 fir16 DATA funnel_ba={hex16(list(reversed(x[1:9])))}",
              f"EX ex03 fir16 DATA funnel_expected={hex16(x[1:9])}",
              "EX ex03 fir16 DATA funnel_verdict ab=1 ba=0",
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
              "EX ex06 fft RESULT ok=10 fail=0"]
    # ex07: the transform, with the same saturating-accumulator model the checker uses.
    mt = [222, 0, 128, 0, 0, 0, 0, 0, 0, 256, 0, 0, 0, 0, 0, 0,
          -128, 0, 222, 0, 0, 0, 0, 0, 0, 0, 0, 256, 0, 0, 0, 0]   # rows padded to 8 lanes
    v7 = [rng.randint(-256, 256) for _ in range(32)]
    out7 = []
    for r in range(4):
        for j in range(8):
            acc = 0
            for k in range(4):
                acc += mt[r * 8 + k] * v7[k * 8 + j]
                acc = max(-(1 << 39), min((1 << 39) - 1, acc))
            out7.append(cel.as_i16(acc >> 16))
    lines += [
        "EX ex07 transform3d BEGIN",
        f"EX ex07 transform3d DATA matrix={hex16(mt)}",
        f"EX ex07 transform3d DATA vertices_soa={hex16(v7)}",
        f"EX ex07 transform3d DATA out={hex16(out7)}",
        f"EX ex07 transform3d DATA ref={hex16(out7)}",
        "EX ex07 transform3d CHECK all_32_transformed_coordinates_match_C pie=32 ref=32 ok",
        "EX ex07 transform3d RESULT ok=1 fail=0",
        "BENCH transform8 vertices=4000 cycles_pie=120000 cycles_c=300000",
    ]
    # ex08: the framebuffer effects. The synthetic log is smaller than the device's 1024 pixels, which is
    # fine: the checker re-derives from whatever the log carries.
    pa = [rng.randint(0, 0x7FFF) for _ in range(32)]
    pb = [rng.randint(0x2000, 0x7FFF) for _ in range(32)]
    lines += [
        "EX ex08 media BEGIN",
        f"EX ex08 media DATA a={hex16(pa)}",
        f"EX ex08 media DATA b={hex16(pb)}",
        f"EX ex08 media DATA half_blend={hex16([cel.as_i16((((x & 0xFFFF) & 0xF7DE) >> 1) + (((y & 0xFFFF) & 0xF7DE) >> 1)) for x, y in zip(pa, pb)])}",
        f"EX ex08 media DATA brightened={hex16([cel.sat16(x + y) for x, y in zip(pa, pb)])}",
        f"EX ex08 media DATA clamped={hex16([max(-1000, min(1000, x)) for x in pa])}",
        f"EX ex08 media DATA tint={hex16([cel.as_i16(((x * 300) >> 8) & 0xFFFF) for x in pa])}",
        f"EX ex08 media DATA shift_in={hex16(pa[:8])}",
        f"EX ex08 media DATA shift_signed={hex16([cel.as_i16(x >> 1) for x in pa[:8]])}",
        f"EX ex08 media DATA shift_unsigned={hex16([cel.as_i16((x & 0xFFFF) >> 1) for x in pa[:8]])}",
        "EX ex08 media DATA limits lo=-1000 hi=1000 tint=300 shift=8 pixels=32",
        "EX ex08 media RESULT ok=5 fail=0",
        "BENCH half_blend pixels=32768 cycles_pie=200000 cycles_c=600000",
        "BENCH brighten pixels=32768 cycles_pie=100000 cycles_c=250000",
    ]
    # ex09: the accumulator probe. Every line here is built from the checker's own models, so the synthetic
    # log is a device log whose answers are already right.
    v8 = cel.EX09_V8
    coef8 = cel.EX09_COEF8
    cro = cel.EX09_COEF_ROWS
    vrw = cel.EX09_V_ROWS
    one_mac = [cel.sat16(x * coef8[0]) for x in v8]
    for i in (1, 2, 3):
        lines.append(f"EX ex09 qacc DATA sel{i}={hex16([cel.sat16(x * coef8[i]) for x in v8])}")
    lines += [f"EX ex09 qacc DATA g{k}={hex16(one_mac)}" for k in ("0", "1", "2", "3", "4", "6", "7")]
    lines += [
        "EX ex09 qacc DATA mac1_min_gap=0 mac1_model_matched_at_g6=1",
        f"EX ex09 qacc DATA mac1_model={hex16(one_mac)}",
        f"EX ex09 qacc DATA zero_vis={hex16([cel.sat16(x * coef8[1]) for x in v8])}",
        f"EX ex09 qacc DATA v8={hex16(v8)}",
        f"EX ex09 qacc DATA coef8={hex16(coef8)}",
        f"EX ex09 qacc DATA coef_rows={hex16([x for row in cro for x in (row + [0, 0, 0, 0])])}",
        f"EX ex09 qacc DATA v_rows={hex16(vrw)}",
    ]
    lanewant = [x * coef8[0] for x in v8]
    for half in ("L", "H"):
        big = 0
        for i, val in enumerate(lanewant[0:4] if half == "L" else lanewant[4:8]):
            big |= (val & ((1 << 40) - 1)) << (40 * i)
        for i in range(5):
            lines.append(f"EX ex09 qacc DATA qacc_{half}_{i}={(big >> (32 * i)) & 0xFFFFFFFF:08x}")
    for i, val in enumerate(lanewant):
        lines.append(f"EX ex09 qacc DATA qacc_lane{i}={val} want={val}")
    for gap in (0, 2, 4):
        lines.append(f"EX ex09 qacc DATA mac4_gap{gap}_matches_model=1")
        lines.append(f"EX ex09 qacc DATA mac4_g{gap}={hex16(cel.mac4_readout(cro[0], vrw))}")
    chain = []
    for r in range(4):
        chain += cel.mac4_readout(cro[r], vrw)
    for key in ("g0", "g4"):
        lines.append(f"EX ex09 qacc DATA chain_{key}_matched=32 chain_{key[1:]}_matched_extra=0 of=32")
        lines.append(f"EX ex09 qacc DATA chain_{key}={hex16(chain)}")
    lines.append(f"EX ex09 qacc DATA chain_model={hex16(chain)}")
    for r in range(4):
        lanes = cel.mac4_raw(cro[r], vrw)
        lines.append(f"EX ex09 qacc DATA raw_row{r}_matched_own=1")
        lines.append(f"EX ex09 qacc DATA raw_row{r}_equals_coef_row={r}")
        lines.append(f"EX ex09 qacc DATA raw_row{r}=" + ",".join(f"{x & 0xFFFF:04x}" for x in lanes))
    lines.append(f"EX ex09 qacc DATA unrolled_matched=32 of=32")
    lines.append(f"EX ex09 qacc DATA unrolled={hex16(chain)}")
    padded = [x for row in cro for x in (row + [0, 0, 0, 0])]
    lines.append(f"EX ex09 qacc DATA ipwalk_matched=32 of=32")
    lines.append(f"EX ex09 qacc DATA ipwalk_out={hex16(padded)}")
    lines.append(f"EX ex09 qacc DATA ipwalk4_out={hex16(padded)}")
    fixed = cel.mac4_readout(cro[0], vrw)
    lines.append("EX ex09 qacc DATA mac_fixed_row0_matched=8 of=8 mac_fixed_rows_identical=1")
    lines.append(f"EX ex09 qacc DATA mac_fixed={hex16(fixed * 4)}")
    wb_a = [cel.sat16(cel.sat40(v8[j] * coef8[0]) >> 5) for j in range(8)]
    lines += [
        f"EX ex09 qacc DATA wb_a={hex16(wb_a)}",
        f"EX ex09 qacc DATA wb_b={hex16(wb_a)}",
        f"EX ex09 qacc DATA wb_model_shift_a={hex16(wb_a)}",
        "EX ex09 qacc DATA srcmb_verdict read_modify_write",
        "EX ex09 qacc DATA probe v=1000..-8000 coef_lanes=3,7,5,11 shift=0",
        "EX ex09 qacc RESULT ok=1 fail=0",
    ]
    lines += ["SUMMARY checks_ok=52 checks_fail=0", "END", ""]
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
    ("ex03 align_probe", "EX ex03 fir16 DATA align_probe=", "0000,0000,0000,0000,0000,0000,0000,0000"),
    ("ex03 funnel", "EX ex03 fir16 DATA funnel_ab=", "0000,0000,0000,0000,0000,0000,0000,0000"),
    ("ex04 interlock", "EX ex04 qr DATA interlock_d0 ", "stall=0.000"),
    ("ex01 raw", "EX ex01 encoding DATA raw_field1_ld=", "3"),
    ("ex07 out", "EX ex07 transform3d DATA out=", "0001"),
    ("ex08 half_blend", "EX ex08 media DATA half_blend=", "0000"),
    ("ex08 shift_unsigned", "EX ex08 media DATA shift_unsigned=", "0000"),
    ("ex08 tint", "EX ex08 media DATA tint=", "0000"),
    ("ex07 matrix stride", "EX ex07 transform3d DATA matrix=", "0000"),
    ("ex09 g0 (the MAC readout at distance 0)", "EX ex09 qacc DATA g0=", "0000"),
    ("ex09 raw_row1 (which coefficient row the MACs used)", "EX ex09 qacc DATA raw_row1=",
     "0000,0000,0000,0000,0000,0000,0000,0000"),
    ("ex09 chain_g0", "EX ex09 qacc DATA chain_g0=", "0000"),
    ("ex09 ipwalk_out (the .IP walk)", "EX ex09 qacc DATA ipwalk_out=", "0000"),
    ("ex09 wb_b (the read-modify-write verdict)", "EX ex09 qacc DATA wb_b=", "0000"),
    ("ex09 qacc_lane3", "EX ex09 qacc DATA qacc_lane3=", "0 want=0"),
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
