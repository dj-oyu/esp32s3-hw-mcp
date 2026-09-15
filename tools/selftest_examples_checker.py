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


def box_img(n_boxes: int, spec: list) -> list[int]:
    """The plane-major AABB image examples.h documents: plane p of box b at 48*(b//8) + 16*p + (b%8), the
    max 8 words on from the min."""
    img = [0] * (48 * ((n_boxes + 7) // 8))
    for b, (mn, mx) in enumerate(spec):
        for ax in range(3):
            img[48 * (b // 8) + 16 * ax + (b % 8)] = mn[ax]
            img[48 * (b // 8) + 16 * ax + 8 + (b % 8)] = mx[ax]
    return img


def point_img(n_points: int, spec: list) -> list[int]:
    """The plane-major point image: axis ax of point i at 24*(i//8) + 8*ax + (i%8)."""
    img = [0] * (24 * ((n_points + 7) // 8))
    for i, xyz in enumerate(spec):
        for ax in range(3):
            img[24 * (i // 8) + 8 * ax + (i % 8)] = xyz[ax]
    return img


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

    # ex10: the two motion kernels. Smaller arrays than the device prints (two blocks of eight lanes), and
    # every value is a reference value from this checker's own models.
    sad_a = [rng.randrange(0, 256) for _ in range(16)]
    sad_b = [rng.randrange(0, 256) for _ in range(16)]
    sad_ref, sad_plain = cel.ref_sad8(sad_a, sad_b), cel.ref_sad8_plain(sad_a, sad_b)
    sad_fa = [0xFFFF, 0x8000, 0x0000, 0x7FFF] + [rng.randrange(0, 0x10000) for _ in range(12)]
    sad_fb = [0x0000, 0x0000, 0xFFFF, 0x8000] + [rng.randrange(0, 0x10000) for _ in range(12)]
    sadf_ref, sadf_plain = cel.ref_sad8(sad_fa, sad_fb), cel.ref_sad8_plain(sad_fa, sad_fb)
    hp_a = [rng.randrange(0, 256) for _ in range(8)]
    hp_b = [rng.randrange(0, 256) for _ in range(8)]
    hp_out = cel.ref_halfpel_formula(hp_a, hp_b)
    fha = [30000, -30000, 20000, -20000, 15000, -15000, 100, -100]
    fhb = [30000, -30000, -20000, 20000, 20000, -20000, 50, -50]
    fh_model = cel.ref_halfpel(fha, fhb)
    fh_expression = cel.ref_halfpel_formula(fha, fhb)
    fh_mismatch = sum(1 for i in range(len(fha)) if fh_model[i] != fh_expression[i])
    fh_pos, fh_neg = cel.halfpel_saturating_lanes(fha, fhb)
    lines += [
        "EX ex10 motion BEGIN",
        f"EX ex10 motion DATA sad_a={hex16(sad_a)}",
        f"EX ex10 motion DATA sad_b={hex16(sad_b)}",
        "EX ex10 motion DATA sad_accx=" + ",".join(f"{w:08x}" for w in
                                                   (sad_ref & 0xFFFFFFFF, sad_ref >> 32)),
        "EX ex10 motion DATA sad_blocks=2",
        f"EX ex10 motion DATA sad_total={sad_ref}",
        f"EX ex10 motion DATA sad_plain_textbook={sad_plain}",
        f"EX ex10 motion DATA sad_straddling_lanes={cel.straddling(sad_a, sad_b)}",
        f"EX ex10 motion DATA sad_clamped_lanes={cel.clamped_diff(sad_a, sad_b)}",
        f"EX ex10 motion DATA sad_full_a={hex16(sad_fa)}",
        f"EX ex10 motion DATA sad_full_b={hex16(sad_fb)}",
        "EX ex10 motion DATA sad_full_accx=" + ",".join(f"{w:08x}" for w in
                                                        (sadf_ref & 0xFFFFFFFF, sadf_ref >> 32)),
        "EX ex10 motion DATA sad_full_blocks=2",
        f"EX ex10 motion DATA sad_full_total={sadf_ref}",
        f"EX ex10 motion DATA sad_full_plain_textbook={sadf_plain}",
        f"EX ex10 motion DATA sad_full_straddling_lanes={cel.straddling(sad_fa, sad_fb)}",
        f"EX ex10 motion DATA sad_full_clamped_lanes={cel.clamped_diff(sad_fa, sad_fb)}",
        f"EX ex10 motion DATA halfpel_a={hex16(hp_a)}",
        f"EX ex10 motion DATA halfpel_b={hex16(hp_b)}",
        f"EX ex10 motion DATA halfpel_out={hex16(hp_out)}",
        "EX ex10 motion DATA halfpel_lanes=8",
        "EX ex10 motion DATA halfpel_ones8=1",
        f"EX ex10 motion DATA halfpel_full_a={hex16(fha)}",
        f"EX ex10 motion DATA halfpel_full_b={hex16(fhb)}",
        f"EX ex10 motion DATA halfpel_full_out={hex16(fh_model)}",
        "EX ex10 motion DATA halfpel_full_lanes=8",
        f"EX ex10 motion DATA halfpel_full_mismatch={fh_mismatch}",
        f"EX ex10 motion DATA halfpel_full_sat_pos={fh_pos}",
        f"EX ex10 motion DATA halfpel_full_sat_neg={fh_neg}",
        f"EX ex10 motion CHECK sad8_8bit_matches_the_saturating_C_reference pie={sad_ref} ref={sad_ref} ok",
        f"EX ex10 motion CHECK sad8_full_range_matches_the_saturating_C_reference pie={sadf_ref} "
        f"ref={sadf_ref} ok",
        "EX ex10 motion CHECK halfpel_8bit_matches_the_reference_expression pie=8 ref=8 ok",
        "EX ex10 motion RESULT ok=4 fail=0",
        "BENCH sad8 blocks=6400 cycles_pie=120000 cycles_c=300000",
        "BENCH halfpel pixels=12800 cycles_pie=30000 cycles_c=90000",
    ]

    # ex11: the 8x8 block transform.
    coef = [rng.randrange(-16384, 16385) for _ in range(64)]
    coeft = [coef[i * 8 + k] for k in range(8) for i in range(8)]
    blk = [rng.randrange(-1024, 1025) for _ in range(64)]
    out11 = cel.ref_block8x8(coef, blk, 15)
    out11t = cel.ref_block8x8(coeft, blk, 15)
    out11s = cel.ref_block8x8(coef, blk, 8)
    sat11 = sum(1 for v in out11s if v in (32767, -32768))
    biggest11 = max(abs(v) for row in cel.block8x8_row_sums(coef, blk) for v in row)
    lines += [
        "EX ex11 block8x8 BEGIN",
        f"EX ex11 block8x8 DATA coef={hex16(coef)}",
        f"EX ex11 block8x8 DATA coef_transposed={hex16(coeft)}",
        f"EX ex11 block8x8 DATA block={hex16(blk)}",
        f"EX ex11 block8x8 DATA out={hex16(out11)}",
        f"EX ex11 block8x8 DATA out_transposed={hex16(out11t)}",
        f"EX ex11 block8x8 DATA out_saturating={hex16(out11s)}",
        "EX ex11 block8x8 DATA shift=15",
        "EX ex11 block8x8 DATA shift_saturating=8",
        f"EX ex11 block8x8 DATA saturated_lanes={sat11}",
        "EX ex11 block8x8 DATA rows_over_40bit=0",
        f"EX ex11 block8x8 DATA max_abs_row_sum={biggest11}",
        "EX ex11 block8x8 CHECK all_64_coefficients_match_the_int64_C_reference pie=64 ref=64 ok",
        "EX ex11 block8x8 RESULT ok=2 fail=0",
        "BENCH block8x8 blocks=2000 cycles_pie=40000 cycles_c=200000",
    ]

    # ex12: integration, the AABB masks and the squared distances. Ten boxes and ten points, so the whole
    # group of eight and the scalar tail both carry data.
    spec_a = [(0, 0, 0), (32767, 0, 0), (26756, 26756, 26756), (26757, 26757, 26757),
              (0, 0, 0), (32767, 32767, 32767), (-1000, 2000, -3000)]
    spec_b = [(0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0),
              (-32768, 0, 0), (-32768, -32768, -32768), (1000, -2000, 3000)]
    pta, ptb = point_img(10, spec_a), point_img(10, spec_b)
    d2 = cel.ref_dist2(pta, ptb, 10)
    d2q = cel.ref_dist2_q16(pta, ptb, 10, 8)
    st = cel.dist2_stats(pta, ptb, 10)
    pos12 = [rng.randrange(-200000, 200001) for _ in range(8)]
    vel12 = [rng.randrange(-200000, 200001) for _ in range(8)]
    acc12 = [rng.randrange(-1000, 1001) for _ in range(8)]
    pos12[6], vel12[6], acc12[6] = 2147483647, 2147483647, -1
    pos12[7], vel12[7], acc12[7] = -2147483648, -2147483648, 1
    wide = cel.ref_integrate(pos12, vel12, acc12, -0x80000000, 0x7FFFFFFF)
    exact = cel.ref_integrate_exact(pos12, vel12, acc12, -0x80000000, 0x7FFFFFFF)
    narrow = cel.ref_integrate(pos12, vel12, acc12, -300000, 300000)
    boxspec = [((-400, -300, -200), (100, 200, 300)),
               ((-400, 500, -200), (100, 600, 300)),
               ((600, -300, -200), (700, 200, 300)),
               ((-400, -300, -200), (100, 200, 300)),
               ((-400, -300, -200), (100, 200, 300)),
               ((-400, -300, -200), (100, 200, 300)),
               ((-400, -300, -200), (100, 200, 300)),
               ((-400, -300, -200), (100, 200, 300)),
               ((-400, -300, -200), (100, 200, 300)),
               ((600, -300, -200), (700, 200, 300))]
    boxes12 = box_img(10, boxspec)
    query12 = [-500, 500, -400, 400, -300, 300]
    masks12 = cel.ref_sat_masks(boxes12, query12, 10)
    lines += [
        "EX ex12 physics BEGIN",
        f"EX ex12 physics DATA pos={hex16(pos12, 8)}",
        f"EX ex12 physics DATA vel={hex16(vel12, 8)}",
        f"EX ex12 physics DATA acc={hex16(acc12, 8)}",
        f"EX ex12 physics DATA integrated={hex16(wide, 8)}",
        f"EX ex12 physics DATA integrated_narrow_bounds={hex16(narrow, 8)}",
        "EX ex12 physics DATA objects=8",
        "EX ex12 physics DATA integrate_lo=-2147483648",
        "EX ex12 physics DATA integrate_hi=2147483647",
        "EX ex12 physics DATA integrate_lo_narrow=-300000",
        "EX ex12 physics DATA integrate_hi_narrow=300000",
        f"EX ex12 physics DATA integrate_diverging_from_exact="
        f"{sum(1 for i in range(8) if wide[i] != exact[i])}",
        f"EX ex12 physics DATA integrate_moved_by_the_bounds="
        f"{sum(1 for i in range(8) if narrow[i] != wide[i])}",
        f"EX ex12 physics DATA boxes={hex16(boxes12)}",
        f"EX ex12 physics DATA query={hex16(query12)}",
        f"EX ex12 physics DATA masks={hex16(masks12)}",
        "EX ex12 physics DATA n_boxes=10",
        f"EX ex12 physics DATA overlap_boxes={sum(1 for v in masks12 if v == -1)}",
        f"EX ex12 physics DATA pt_a={hex16(pta)}",
        f"EX ex12 physics DATA pt_b={hex16(ptb)}",
        f"EX ex12 physics DATA dist2={hex16(d2, 8)}",
        "EX ex12 physics DATA n_points=10",
        f"EX ex12 physics DATA dist2_int32_clamps={st['int32_clamps']}",
        f"EX ex12 physics DATA dist2_saturating_axis_diffs={st['saturating_axis_diffs']}",
        f"EX ex12 physics DATA dist2_max_abs_delta={st['max_abs_delta']}",
        f"EX ex12 physics DATA dist2_changed_by_the_saturating_difference={st['changed_by_saturation']}",
        f"EX ex12 physics DATA dist2_q16={hex16(d2q)}",
        "EX ex12 physics DATA q16_shift=8",
        f"EX ex12 physics DATA q16_written={len(d2q)}",
        f"EX ex12 physics DATA q16_saturated_lanes={sum(1 for v in d2q if v in (32767, -32768))}",
        "EX ex12 physics CHECK sat_masks_matches_the_C_reference pie=10 ref=10 ok",
        "EX ex12 physics CHECK dist2_matches_the_C_reference pie=10 ref=10 ok",
        "EX ex12 physics RESULT ok=5 fail=0",
        "BENCH integrate objects=16000 cycles_pie=320000 cycles_c=640000",
        "BENCH sat_masks boxes=38000 cycles_pie=400000 cycles_c=900000",
        "BENCH dist2 pairs=38000 cycles_pie=700000 cycles_c=500000",
        "BENCH dist2_q16 pairs=32000 cycles_pie=200000 cycles_c=450000",
    ]
    lines += ["SUMMARY checks_ok=69 checks_fail=0", "END", ""]
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
    ("ex10 sad_accx (the 40-bit RUR.ACCX_0/1 readout)", "EX ex10 motion DATA sad_accx=",
     "00000000,00000000"),
    ("ex10 halfpel_out (the half-pel row)", "EX ex10 motion DATA halfpel_out=", "0000"),
    ("ex10 halfpel_full_mismatch (the saturating rounding add)", "EX ex10 motion DATA halfpel_full_mismatch=",
     "0"),
    ("ex11 out (the 8x8 transform at shift 15)", "EX ex11 block8x8 DATA out=", "0000"),
    ("ex11 out_saturating (the saturating readout)", "EX ex11 block8x8 DATA out_saturating=", "0000"),
    ("ex12 integrated (the saturating adds and the clamp)", "EX ex12 physics DATA integrated=", "00000000"),
    ("ex12 masks (the separating-axis result)", "EX ex12 physics DATA masks=", "0000"),
    ("ex12 dist2 (the QACC squared distances)", "EX ex12 physics DATA dist2=", "00000000"),
    ("ex12 dist2_q16 (the one-instruction readout)", "EX ex12 physics DATA dist2_q16=", "0000"),
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
