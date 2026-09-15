#!/usr/bin/env python3
"""Check a log from the PIE examples (examples/firmware) against references computed here.

The examples firmware checks itself against a scalar C reference written from the same manual pseudo-code;
this checker is the third opinion, and the one that matters when the two agree for the wrong reason. Every
value it can is re-derived in Python from the inputs the firmware printed on its DATA lines.

    .venv/bin/python tools/check_examples_log.py /workspace/backups/pie-examples-<stamp>.log
    .venv/bin/python tools/check_examples_log.py <log> --json report.json

Exit code 0 means every check that the manual can predict came out as predicted. The checks that cannot be
predicted -- the places where the manual and the assembler disagree -- are reported as a decision instead:
which of the competing readings the silicon matched (ex01). That decision is the point of the example, so
it does not fail the run, it prints which document was right.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

DATA_RE = re.compile(r"^EX (\S+) (\S+) DATA (\S+)=(.*)$")
CHECK_RE = re.compile(r"^EX (\S+) (\S+) CHECK (\S+) pie=(-?\d+) ref=(-?\d+) (ok|FAIL)$")
RESULT_RE = re.compile(r"^EX (\S+) (\S+) RESULT ok=(\d+) fail=(\d+)$")
INTERLOCK_RE = re.compile(
    r"^EX (\S+) (\S+) DATA interlock_(\S+) stall=([-\d.]+) dep=(\d+) ind=(\d+) iters=(\d+)$")
BENCH_RE = re.compile(r"^BENCH (\S+) (\S+)=(\d+) cycles_pie=(\d+) cycles_c=(\d+)$")


def parse(path: str) -> dict:
    """{'ex01': {'data': {...}, 'checks': [...], 'results': [...], 'interlock': {...}}, ...}"""
    sections: dict[str, dict] = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line.startswith(("EX ", "BENCH ")):
            continue
        m = BENCH_RE.match(line)
        if m:
            kernel = m.group(1)
            sec = sections.setdefault("__bench__", {"data": {}, "checks": [], "results": [],
                                                    "interlock": {}, "bench": {}})
            sec["bench"][kernel] = {"elements": int(m.group(3)), "cycles_pie": int(m.group(4)),
                                    "cycles_c": int(m.group(5))}
            continue
        ex = line.split()[1]
        sec = sections.setdefault(ex, {"data": {}, "checks": [], "results": [], "interlock": {},
                                      "bench": {}})
        m = INTERLOCK_RE.match(line)
        if m:
            sec["interlock"][m.group(3)] = {"stall": float(m.group(4)), "dep": int(m.group(5)),
                                            "ind": int(m.group(6)), "iters": int(m.group(7))}
            continue
        m = CHECK_RE.match(line)
        if m:
            sec["checks"].append({"label": m.group(3), "pie": int(m.group(4)), "ref": int(m.group(5)),
                                  "ok": m.group(6) == "ok"})
            continue
        m = RESULT_RE.match(line)
        if m:
            sec["results"].append({"ok": int(m.group(3)), "fail": int(m.group(4))})
            continue
        m = DATA_RE.match(line)
        if m:
            sec["data"][m.group(3)] = m.group(4)
    return sections


def as_i16(x: int) -> int:
    x &= 0xFFFF
    return x - 0x10000 if x >= 0x8000 else x


def as_i32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def sat32(v: int) -> int:
    return max(-0x80000000, min(0x7FFFFFFF, v))


def sat16(v: int) -> int:
    return max(-0x8000, min(0x7FFF, v))


def hex_words(text: str) -> list[int]:
    return [as_i32(int(w, 16)) for w in text.split(",") if w.strip()]


def i16_words(text: str) -> list[int]:
    return [as_i16(int(w, 16)) for w in text.split(",") if w.strip()]


# ------------------------------------------------------------------ references (all pure Python, from the manual)

def ref_matmul(a: list[int], bt: list[int]) -> list[int]:
    c = []
    for i in range(16):
        for j in range(16):
            acc = sum(a[i * 16 + k] * bt[j * 16 + k] for k in range(16))
            c.append(sat32(acc))
    return c


def ref_fir(x: list[int], h: list[int], n_out: int, shift: int) -> list[int]:
    return [sat32(sum(x[n + k] * h[k] for k in range(16)) >> shift) for n in range(n_out)]


def ref_accx(iterations: int, lane_value: int) -> tuple[int, int]:
    """(clamped 40-bit accumulator, mathematical sum) per the manual's saturating pseudo-code."""
    per_iteration = 8 * lane_value * lane_value
    acc, acc_max = 0, (1 << 39) - 1
    for _ in range(iterations):
        acc += per_iteration
        if acc > acc_max:
            acc = acc_max
    return acc, per_iteration * iterations


def ref_r2bf(x: list[int], sel2: int) -> list[int]:
    """The manual's op_a/op_b construction for EE.FFT.R2BF.S16, with qx = qy = x (8 lanes).

    The operand lists are written MSB first: for {qy[95:64], qy[31:0], qx[95:64], qx[31:0]} the leftmost
    32-bit field lands in the *highest* lanes. Reading them the other way round swaps the two halves of qa0,
    which is what the first silicon run (log pie-examples-20260914T174138Z) showed, and with the MSB-first
    reading the hardware's lanes come out exactly as this function predicts.
    """
    qx, qy = x[:8], x[:8]
    if sel2 == 0:
        op_a = [qx[0], qx[1], qx[2], qx[3], qy[0], qy[1], qy[2], qy[3]]
        op_b = [qx[4], qx[5], qx[6], qx[7], qy[4], qy[5], qy[6], qy[7]]
    else:
        op_a = [qx[0], qx[1], qx[4], qx[5], qy[0], qy[1], qy[4], qy[5]]
        op_b = [qx[2], qx[3], qx[6], qx[7], qy[2], qy[3], qy[6], qy[7]]
    return [as_i16(op_a[i] + op_b[i]) for i in range(4)] + [as_i16(op_a[i] - op_b[i]) for i in range(4)]


def ref_cmul(u: list[int], v: list[int], sar: int, half: int) -> list[int]:
    """EE.CMUL.S16 qz, qx, qy, sel4: two complex multiplies on (re,im) lane pairs, shifted by SAR."""
    out = [0] * 8
    for pair in range(2 * half, 2 * half + 2):
        re, im = u[pair * 2], u[pair * 2 + 1]
        cre, cim = v[pair * 2], v[pair * 2 + 1]
        out[pair * 2] = as_i16((re * cre - im * cim) >> sar)
        out[pair * 2 + 1] = as_i16((re * cim + im * cre) >> sar)
    return out


# The inputs the firmware hard-codes for the checks that have no DATA line of their own.
EX01_SRS = [(((1 << 38) + 12345), 0), (((1 << 38) + 12345), 8), ((-((1 << 38) + 999)), 5)]
EX06_U = [1000, 2000, 3000, -4000, 7000, 8000, 9000, -10000]
EX06_V = [500, -600, 700, 800, -900, 1000, 1100, 1200]
EX06_X = [100, -200, 300, -400, 500, -600, 700, -800]


# ------------------------------------------------------------------ per-example checks

def check_ex01(sec: dict) -> list[tuple[str, bool, str]]:
    """The address steps are measurements, so what is checkable here is internal consistency; the
    raw-field probe decides which document the silicon follows, and prints that as the finding."""
    out = []
    d = sec["data"]
    if "raw_field1_ld" not in d or "raw_field1_st" not in d:
        return [("ex01 raw-field probe present", False, "no raw_field1_ld/raw_field1_st lines")]
    raw_ld, raw_st = int(d["raw_field1_ld"]), int(d["raw_field1_st"])
    scale = {8: "the assembler and the LD form's printed pseudo-code (8-byte units)",
             4: "the printed syntax range of EE.ST.ACCX.IP (4-byte units)",
             1: "the printed pseudo-code of EE.ST.ACCX.IP (1-byte units)"}
    for what, value in (("EE.LD.ACCX.IP", raw_ld), ("EE.ST.ACCX.IP", raw_st)):
        out.append((f"{what} with field=1 advances its address register by {value} bytes", value in scale,
                    f"the silicon follows {scale.get(value, 'none of the documented readings')}"))
    for key in ("step_ld_accx+16", "step_st_accx+16", "step_ld128+16", "step_vld128+16"):
        if key in d:
            got = int(d[key])
            out.append((f"{key.split('step_')[1]}: the printed immediate moves the address by {got}",
                        got == 16, f"expected 16, got {got}"))
    # EE.SRS.ACCX: ACCX >> shift, saturated into 32 bits. Re-derived from the inputs the firmware carries
    # in its source (they are constants of the example, not of the log).
    srs = [c["pie"] for c in sec["checks"] if c["label"].startswith("srs_accx")]
    want = [sat32(value >> shift) for value, shift in EX01_SRS]
    if not srs:
        out.append(("ex01 carries its EE.SRS.ACCX readouts", False, "no srs_accx CHECK lines"))
    else:
        out.append((f"ex01 EE.SRS.ACCX readouts {srs} match sat32(ACCX >> shift) {want}", srs == want,
                    "" if srs == want else f"got {srs}, expected {want}"))
    return out


def check_ex02(sec: dict) -> list[tuple[str, bool, str]]:
    d = sec["data"]
    if not {"c", "a", "bt"} <= set(d):
        return [("ex02 carries its inputs and results", False, "missing a/bt/c")]
    a, bt = i16_words(d["a"]), i16_words(d["bt"])
    if len(a) != 256 or len(bt) != 256:
        return [("ex02 printed the full 16x16 inputs", False, f"a={len(a)} bt={len(bt)} words")]
    pie, ref = hex_words(d["c"]), ref_matmul(a, bt)
    bad = [(i, pie[i], ref[i]) for i in range(len(pie)) if pie[i] != ref[i]]
    return [("ex02 all 256 products match the Python reference", not bad,
             f"{len(bad)} mismatches, first {bad[:3]}" if bad else "256/256")]


def check_ex03(sec: dict) -> list[tuple[str, bool, str]]:
    d = sec["data"]
    if not {"x", "h"} <= set(d):
        return [("ex03 carries its inputs", False, "missing x/h")]
    x, h = i16_words(d["x"]), i16_words(d["h"])
    out = []
    for key, shift in (("y_raw", 0), ("y_q15", 15)):
        if key not in d:
            out.append((f"ex03 {key} present", False, "missing"))
            continue
        pie = hex_words(d[key])
        ref = ref_fir(x, h, len(pie), shift)
        bad = [(i, pie[i], ref[i]) for i in range(len(pie)) if pie[i] != ref[i]]
        out.append((f"ex03 FIR shift={shift} matches the Python reference", not bad,
                    f"{len(bad)} mismatches, first {bad[:3]}" if bad else f"{len(pie)}/{len(pie)}"))
    # The alignment rule that forced the kernel's shape, kept as a measurement rather than a comment.
    if "align_probe" in d:
        words = i16_words(d["align_probe"])
        lo, hi = words[:8], words[8:]
        out.append(("ex03 a 128-bit load at x+2 returns the bytes at x (the low address bits are dropped)",
                    lo == hi and any(lo), f"{lo} vs {hi}"))
    # The no-copy path: EE.LD.128.USAR.IP leaves the dropped bits in SAR_BYTE, EE.SRC.Q shifts the window
    # out of two aligned chunks. The syntax line for EE.SRC.Q does not extract, so the log decides which
    # operand is qs0; what is checkable is that one of the two orders produces the window at byte 2.
    if {"funnel_ab", "funnel_ba", "x"} <= set(d):
        x = i16_words(d["x"])
        expected = x[1:9]                      # x is 16-byte aligned, so byte offset 2 is x[1]
        ab, ba = i16_words(d["funnel_ab"]), i16_words(d["funnel_ba"])
        if ab == expected:
            which = "EE.SRC.Q qz, q0, q1 (the first operand is qs0)"
        elif ba == expected:
            which = "EE.SRC.Q qz, q1, q0 (the second operand is qs0)"
        else:
            which = "neither order"
        out.append(("ex03 EE.SRC.Q reproduces the window at byte 2 in one operand order",
                    which != "neither order",
                    f"{which}; expected {expected}, got ab={ab} ba={ba}"))
    return out


def check_ex04(sec: dict) -> list[tuple[str, bool, str]]:
    out = []
    for label, expect in (("d0", 1), ("d1", 0), ("d2", 0)):
        h = sec["interlock"].get(label)
        if h is None:
            out.append((f"ex04 interlock {label} present", False, "missing"))
            continue
        stall = (h["dep"] - h["ind"]) / h["iters"]
        got = 1 if stall > 0.5 else 0
        out.append((f"ex04 LD.QR interlock at issue distance {label}: {stall:.3f} cycles per iteration",
                    got == expect,
                    f"expected {'a stall' if expect else 'no stall'}, got {got} "
                    f"({h['dep']} - {h['ind']} over {h['iters']} iterations)"))
    return out


def check_ex05(sec: dict) -> list[tuple[str, bool, str]]:
    d = sec["data"]
    if not {"readout_sat32", "readout_shifted9", "clamped_40bit", "mathematical_sum"} <= set(d):
        return [("ex05 carries its readouts", False,
                 "missing readout_sat32/readout_shifted9/clamped_40bit/mathematical_sum")]
    sat_got, shifted_got = int(d["readout_sat32"]), int(d["readout_shifted9"])
    acc = int(d["clamped_40bit"])
    maths = int(d["mathematical_sum"])
    return [
        ("ex05 the accumulator clamped below the mathematical sum", acc < maths,
         f"clamped {acc} < {maths}"),
        ("ex05 ACCX >> 9 readout matches the clamped accumulator", shifted_got == acc >> 9,
         f"got {shifted_got}, expected {acc >> 9}"),
        ("ex05 sat32 readout is the 32-bit clamp of the accumulator", sat_got == sat32(acc),
         f"got {sat_got}, expected {sat32(acc)}"),
    ]


def check_ex06(sec: dict) -> list[tuple[str, bool, str]]:
    d = sec["data"]
    out = []
    for key, sel2 in (("r2bf_sel0_out", 0), ("r2bf_sel1_out", 1)):
        if key in d:
            got = i16_words(d[key])
            ref = ref_r2bf(EX06_X, sel2)
            out.append((f"ex06 R2BF sel2={sel2} reproduces the pseudo-code packing", got == ref,
                        f"got {got} expected {ref}"))
    for sar in (0, 12):
        for half in (0, 1):
            key = f"cmul_half{half}_sar{sar}"
            if key not in d:
                continue
            got = i16_words(d[key])
            ref = ref_cmul(EX06_U, EX06_V, sar, half)
            lo, hi = 4 * half, 4 * half + 4
            exp = ref[lo:hi]
            out.append((f"ex06 CMUL sel4={half} sar={sar} matches the pseudo-code", got == exp,
                        f"got {got} expected {exp}"))
    return out


def check_ex07(sec: dict) -> list[tuple[str, bool, str]]:
    """The 4x4 transform: eight vertices at once, against a Python model of the same arithmetic (including
    the saturating 40-bit accumulator the VSMULAS family documents)."""
    d = sec["data"]
    if not {"matrix", "vertices_soa", "out"} <= set(d):
        return [("ex07 carries matrix, vertices and result", False, "missing DATA lines")]
    m, v = i16_words(d["matrix"]), i16_words(d["vertices_soa"])
    got = i16_words(d["out"])
    if len(m) < 32 or len(v) < 32:
        return [("ex07 carries the padded matrix and the eight-vertex rows", False,
                 f"matrix {len(m)} values, vertices {len(v)} values (expected 32 each)")]
    ref = []
    for r in range(4):
        for j in range(8):
            acc = 0
            for k in range(4):
                acc += m[r * 8 + k] * v[k * 8 + j]     # matrix rows are padded to eight lanes
                acc = max(-(1 << 39), min((1 << 39) - 1, acc))
            ref.append(as_i16(acc >> 16))
    bad = [(i, got[i], ref[i]) for i in range(len(got)) if got[i] != ref[i]]
    return [("ex07 all 32 transformed coordinates match the Python model", not bad,
             f"{len(bad)} mismatches, first {bad[:3]}" if bad else "32/32")]


def check_ex08(sec: dict) -> list[tuple[str, bool, str]]:
    """The framebuffer effects, re-derived from the pixel arrays the log carries.

    The half blend is UNSIGNED: the lanes are packed RGB565 and the kernel shifts them with EE.VMUL.U16.
    The first device run of this example used EE.VMUL.S16 there and every lane whose bit 15 was set came
    back sign-extended -- which is what `shift_signed`/`shift_unsigned` below now pins down in the log.
    """
    d = sec["data"]
    if not {"a", "b"} <= set(d):
        return [("ex08 carries its pixel rows", False, "missing a/b")]
    a, b = i16_words(d["a"]), i16_words(d["b"])
    out = []
    if "half_blend" in d:
        got = i16_words(d["half_blend"])
        ref = [as_i16((((x & 0xFFFF) & 0xF7DE) >> 1) + (((y & 0xFFFF) & 0xF7DE) >> 1)) for x, y in zip(a, b)]
        bad = [(i, got[i], ref[i]) for i in range(len(got)) if got[i] != ref[i]]
        out.append(("ex08 half_blend (unsigned mask-shift-add) matches the Python reference", not bad,
                    f"{len(bad)} mismatches, first {bad[:3]}" if bad else f"{len(got)}/{len(got)}"))
    if {"shift_in", "shift_signed", "shift_unsigned"} <= set(d):
        xs = i16_words(d["shift_in"])
        sg, us = i16_words(d["shift_signed"]), i16_words(d["shift_unsigned"])
        ref_s = [as_i16(x >> 1) for x in xs]                       # EE.VMUL.S16: arithmetic
        ref_u = [as_i16((x & 0xFFFF) >> 1) for x in xs]            # EE.VMUL.U16: logical
        out.append(("ex08 signed multiply shifts in the sign, unsigned does not", sg == ref_s and us == ref_u,
                    f"signed {sg[:3]} vs {ref_s[:3]}, unsigned {us[:3]} vs {ref_u[:3]}"))
    if "brightened" in d:
        got = i16_words(d["brightened"])
        ref = [sat16(x + y) for x, y in zip(a, b)]
        bad = [(i, got[i], ref[i]) for i in range(len(got)) if got[i] != ref[i]]
        out.append(("ex08 brighten saturates exactly like the reference", not bad,
                    f"{len(bad)} mismatches, first {bad[:3]}" if bad else f"{len(got)}/{len(got)}"))
    if "clamped" in d:
        got = i16_words(d["clamped"])
        ref = [max(-1000, min(1000, x)) for x in a]
        bad = [(i, got[i], ref[i]) for i in range(len(got)) if got[i] != ref[i]]
        out.append(("ex08 clamp matches the reference", not bad,
                    f"{len(bad)} mismatches, first {bad[:3]}" if bad else f"{len(got)}/{len(got)}"))
    if "tint" in d:
        got = i16_words(d["tint"])
        ref = [as_i16(((x * 300) >> 8) & 0xFFFF) for x in a]     # EE.VMUL truncates, it does not saturate
        bad = [(i, got[i], ref[i]) for i in range(len(got)) if got[i] != ref[i]]
        out.append(("ex08 tint (per-lane multiply with a SAR shift, truncating) matches the reference",
                    not bad, f"{len(bad)} mismatches, first {bad[:3]}" if bad else f"{len(got)}/{len(got)}"))
    return out


# ------------------------------------------------------------------ ex09: the accumulator probe

EX09_V8 = [1000, -2000, 3000, -4000, 5000, -6000, 7000, -8000]
EX09_COEF8 = [3, 7, 5, 11, 0, 0, 0, 0]
EX09_COEF_ROWS = [[3, 7, 5, 11], [0, -2, 0, 4], [1, 0, 0, 2], [0, 0, 4, 0]]
EX09_V_ROWS = [1000, -2000, 3000, -4000, 5000, -6000, 7000, -8000,
               500, -500, 500, -500, 250, -250, 250, -250,
               1000, 1000, -1000, -1000, 1000, 1000, -1000, -1000,
               3, 5, 7, 11, 13, 17, 19, 23]


def sat40(v: int) -> int:
    return max(-(1 << 39), min((1 << 39) - 1, v))


def mac4_raw(coef_row: list[int], v_rows: list[int]) -> list[int]:
    """The four-MAC row without the readout: lane j accumulates coef[k] * v[k][j], saturating at 40 bits."""
    acc = []
    for j in range(8):
        a = 0
        for k in range(4):
            a = sat40(a + coef_row[k] * v_rows[k * 8 + j])
        acc.append(a)
    return acc


def mac4_readout(coef_row: list[int], v_rows: list[int], shift: int = 0) -> list[int]:
    return [sat16(a >> shift) for a in mac4_raw(coef_row, v_rows)]


def qacc_lane(words: list[int], i: int) -> int:
    """Lane i of a 160-bit accumulator register held as five 32-bit RUR words."""
    bit = i * 40
    pair = words[bit // 32] | (words[bit // 32 + 1] << 32)
    raw = (pair >> (bit % 32)) & ((1 << 40) - 1)
    return raw - (1 << 40) if raw >= (1 << 39) else raw


def check_ex09(sec: dict) -> list[tuple[str, bool, str]]:
    """The accumulator probe, re-derived here. Everything the firmware checks is recomputed, and the two
    questions the manual does not answer (does the readout need spacing, does SRCMB write QACC back) are
    reported as a decision rather than as a pass/fail of the manual."""
    d = sec["data"]
    out: list[tuple[str, bool, str]] = []
    if not {"g0", "qacc_lane0"} <= set(d):
        return [("ex09 carries the probe's data lines", False, "missing g0/qacc_lane0")]
    v8 = i16_words(d["v8"]) if "v8" in d else EX09_V8
    coef8 = i16_words(d["coef8"]) if "coef8" in d else EX09_COEF8
    if "coef_rows" in d:
        flat = i16_words(d["coef_rows"])
        coef_rows = [flat[r * 8:r * 8 + 4] for r in range(4)]
    else:
        coef_rows = EX09_COEF_ROWS
    v_rows = i16_words(d["v_rows"]) if "v_rows" in d else EX09_V_ROWS

    # 1. one MAC: is the accumulator visible to the readout at issue distance 0?
    model = [sat16(x * coef8[0]) for x in v8]
    gaps = [k for k in ("g0", "g1", "g2", "g3", "g4", "g6", "g7") if k in d]
    wrong = []
    min_ok = None
    for k in gaps:
        same = i16_words(d[k]) == model
        if not same:
            wrong.append(k)
        elif min_ok is None:
            min_ok = k
    out.append(("ex09 table 1.7-2 lists no accumulator def, yet the readout at distance 0 is correct",
                min_ok == "g0", f"first gap that matches the model: {min_ok}; gaps that differ: {wrong}"))

    # 2. which lane sel8 broadcasts
    for sel, key in ((1, "sel1"), (2, "sel2"), (3, "sel3")):
        if key in d:
            want = [sat16(x * coef8[sel]) for x in v8]
            got = i16_words(d[key])
            out.append((f"ex09 sel8 = {sel} broadcasts coefficient lane {sel}", got == want,
                        f"got {got[:4]} want {want[:4]}"))

    # 3. the raw accumulator: lanes 0-3 in QACC_L, 4-7 in QACC_H, 40 bits each
    if all(f"qacc_{h}_{i}" in d for h in ("L", "H") for i in range(5)):
        words = {h: [int(d[f"qacc_{h}_{i}"], 16) for i in range(5)] for h in ("L", "H")}
        lanes = [qacc_lane(words["L"], i) for i in range(4)] + [qacc_lane(words["H"], i) for i in range(4)]
        want = [x * coef8[0] for x in v8]
        out.append(("ex09 the raw 40-bit lanes are (QACC_H_i[7:0] << 32) | QACC_L_i", lanes == want,
                    f"decoded {lanes} want {want}"))
        # the firmware decodes the same words on the device: two independent readings of one 320-bit value
        fw = []
        for i in range(8):
            text = d.get(f"qacc_lane{i}", "")
            fw.append(int(text.split("want=")[0]) if "want=" in text else None)
        out.append(("ex09 the firmware's own lane decode agrees with this one", fw == want,
                    f"device {fw[:4]} host {want[:4]}"))

    # 4. ZERO.QACC between two MACs
    if "zero_vis" in d:
        want = [sat16(x * coef8[1]) for x in v8]
        got = i16_words(d["zero_vis"])
        out.append(("ex09 EE.ZERO.QACC between two MACs is ordered", got == want,
                    f"got {got[:3]} want {want[:3]} (v*(coef[0]+coef[1]) would mean it is not)"))

    # 5. four MACs, at three readout distances
    m4 = mac4_readout(coef_rows[0], v_rows)
    for key, gap in (("mac4_g0", 0), ("mac4_g2", 2), ("mac4_g4", 4)):
        if key in d:
            got = i16_words(d[key])
            out.append((f"ex09 four MACs with the readout {gap} slots later match the model", got == m4,
                        "match" if got == m4 else f"got {got[:3]} want {m4[:3]}"))

    # 6. the looped version, and the model row by row
    chain_model = []
    for r in range(4):
        chain_model += mac4_readout(coef_rows[r], v_rows)
    for key in ("chain_g0", "chain_g4"):
        if key in d:
            got = i16_words(d[key])
            bad = [i for i in range(len(got)) if got[i] != chain_model[i]]
            out.append((f"ex09 the looped row sequence ({key[-2:]}) matches the model for all four rows",
                        not bad, f"{len(bad)} lanes differ, first {bad[:3]}" if bad else "32/32"))

    # 7. what the MACs consumed, row by row (no readout in the picture at all)
    raw_ok = True
    raw_detail = []
    for r in range(4):
        key = f"raw_row{r}"
        if key not in d:
            continue
        lanes = i16_words(d[key])
        want = mac4_raw(coef_rows[r], v_rows)
        own = lanes == want
        raw_ok &= own
        which = d.get(f"raw_row{r}_equals_coef_row", "?")
        raw_detail.append(f"row{r}: " + ("own row" if own else f"equals coef row {which}"))
    if raw_detail:
        out.append(("ex09 each raw accumulator row holds its own coefficient row's sums", raw_ok,
                    " ".join(raw_detail)))

    # 8. the coefficient row layout trap, and the address walk on its own
    if "ipwalk_out" in d:
        got = i16_words(d["ipwalk_out"])
        want = [x for row in coef_rows for x in row] + [0] * 16
        flat = i16_words(d["coef_rows"]) if "coef_rows" in d else want
        out.append(("ex09 the 128-bit .IP walk advances 16 bytes per step (padded rows)", got == flat,
                    "the walk reproduces the padded array" if got == flat else f"got {got[:6]} want {flat[:6]}"))

    # 9. the coefficient register held in a register instead of reloaded per row
    if "mac_fixed" in d:
        got = i16_words(d["mac_fixed"])
        want = mac4_readout(coef_rows[0], v_rows)
        rows = [got[r * 8:(r + 1) * 8] for r in range(4)]
        out.append(("ex09 with the coefficient held in a register every row repeats the model",
                    all(r == want for r in rows), f"rows {rows[0][:3]} ..." if rows else ""))

    # 10. the readout's write-back
    if {"wb_a", "wb_b"} <= set(d):
        want_a = [sat16(sat40((v8[j] * coef8[0])) >> 5) for j in range(8)]
        got_a, got_b = i16_words(d["wb_a"]), i16_words(d["wb_b"])
        rmw = got_b == got_a
        single = got_b == [sat16(v8[j] * coef8[0]) for j in range(8)]
        out.append(("ex09 EE.SRCMB.S16.QACC writes the shifted values back into the accumulator", rmw,
                    f"second readout = {'the first readout (read-modify-write)' if rmw else ('the raw accumulator (single shot)' if single else 'neither')}"))
        out.append(("ex09 the first readout is ACCX >> 5 saturated to 16 bits", got_a == want_a,
                    f"got {got_a[:3]} want {want_a[:3]}"))
    return out


# ------------------------------------------------------------------ ex10: motion (block SAD + half-pel row)

def u16_words(text: str) -> list[int]:
    """The lanes of ex10's SAD are uint16 bit patterns: i16_words would fold the top half down, and the
    textbook SAD that says where the kernel's inputs leave their contract is computed on the unsigned
    reading."""
    return [int(w, 16) & 0xFFFF for w in text.split(",") if w.strip()]


def accx_total(text: str) -> int:
    """((uint64_t)ACCX[39:32] << 32) | ACCX[31:0] from the two words RUR.ACCX_0/1 returned."""
    w = [int(x, 16) & 0xFFFFFFFF for x in text.split(",") if x.strip()]
    return -1 if len(w) < 2 else (w[0] | (w[1] << 32))


def ref_sad8_lane(a: int, b: int) -> int:
    """One lane through the vector unit: VSUBS.S16(VMAX.S16, VMIN.S16) over the SIGNED lane readings, i.e.
    min(<|a-b|, or 65536-|a-b| when the two lanes straddle 32768>, 32767)."""
    d = abs(as_i16(a) - as_i16(b))
    return 32767 if d > 32767 else d


def ref_sad8(a: list[int], b: list[int]) -> int:
    return sum(ref_sad8_lane(x, y) for x, y in zip(a, b))


def ref_sad8_rule(a: list[int], b: list[int]) -> int:
    """The same total stated the way ex10_motion.S states the rule: the UNSIGNED difference, or its
    65536-complement when the two lanes sit in different halves, clamped at 32767. Independent of
    ref_sad8's signed-reading formulation, which it must agree with."""
    total = 0
    for x, y in zip(a, b):
        d = abs(x - y)
        if (x >= 0x8000) != (y >= 0x8000):
            d = 0x10000 - d
        total += 32767 if d > 32767 else d
    return total


def ref_sad8_plain(a: list[int], b: list[int]) -> int:
    """The textbook SAD on the unsigned readings -- equal to the kernel inside the 8-bit sample domain."""
    return sum(abs(x - y) for x, y in zip(a, b))


def straddling(a: list[int], b: list[int]) -> int:
    return sum(1 for x, y in zip(a, b) if (x >= 0x8000) != (y >= 0x8000))


def clamped_diff(a: list[int], b: list[int]) -> int:
    return sum(1 for x, y in zip(a, b) if abs(as_i16(x) - as_i16(y)) > 32767)


def ref_halfpel(a: list[int], b: list[int], ones: int = 1, sar: int = 1) -> list[int]:
    """The kernel's own sequence: VADDS.S16 (the sum), VADDS.S16 (+ones), VMUL.U16 by ones with SAR = 1 --
    all three saturating/flags as the manual's pseudo-code has them."""
    out = []
    for x, y in zip(a, b):
        t = sat16(sat16(x + y) + ones)
        out.append(as_i16((((t & 0xFFFF) * (ones & 0xFFFF)) >> sar) & 0xFFFF))
    return out


def ref_halfpel_formula(a: list[int], b: list[int]) -> list[int]:
    """out[i] = (uint16_t)(a[i] + b[i] + 1) >> 1 -- the expression the kernel is written from."""
    return [as_i16(((x + y + 1) & 0xFFFF) >> 1) for x, y in zip(a, b)]


def halfpel_saturating_lanes(a: list[int], b: list[int]) -> tuple[int, int]:
    """The lanes where a + b + 1 does not fit a signed 16-bit lane, positive and negative."""
    pos = sum(1 for x, y in zip(a, b) if x + y + 1 > 32767)
    neg = sum(1 for x, y in zip(a, b) if x + y + 1 < -32768)
    return pos, neg


def check_ex10(sec: dict) -> list[tuple[str, bool, str]]:
    """The two motion kernels, re-derived from the DATA lines: the block SAD against a third reading of the
    instruction rule (VMAX/VMIN/VSUBS.S16 into VMULAS.U16.ACCX, read out once through RUR.ACCX_0/1) and the
    half-pel row against both the kernel's op sequence and the reference expression."""
    d = sec["data"]
    need = {"sad_a", "sad_b", "sad_accx", "sad_total", "sad_plain_textbook", "sad_blocks",
            "sad_straddling_lanes", "sad_clamped_lanes", "sad_full_a", "sad_full_b", "sad_full_accx",
            "sad_full_total", "sad_full_blocks", "sad_full_plain_textbook", "sad_full_straddling_lanes",
            "sad_full_clamped_lanes", "halfpel_a", "halfpel_b", "halfpel_out", "halfpel_lanes",
            "halfpel_ones8", "halfpel_full_a",
            "halfpel_full_b", "halfpel_full_out", "halfpel_full_lanes", "halfpel_full_mismatch",
            "halfpel_full_sat_pos", "halfpel_full_sat_neg"}
    missing = sorted(need - set(d))
    if missing:
        return [("ex10 carries the SAD and half-pel inputs and results", False, f"missing {missing}")]
    out: list[tuple[str, bool, str]] = []

    a, b = u16_words(d["sad_a"]), u16_words(d["sad_b"])
    fa, fb = u16_words(d["sad_full_a"]), u16_words(d["sad_full_b"])
    ref8, ref_full = ref_sad8(a, b), ref_sad8(fa, fb)
    plain8, plain_full = ref_sad8_plain(a, b), ref_sad8_plain(fa, fb)
    blocks, full_blocks = int(d["sad_blocks"]), int(d["sad_full_blocks"])
    out.append((f"ex10 the SAD inputs are the {blocks} blocks of eight lanes the kernel was told to read",
                len(a) == len(b) == 8 * blocks, f"a={len(a)} b={len(b)} blocks={blocks}"))
    out.append((f"ex10 the full-range inputs are the {full_blocks} blocks of eight lanes",
                len(fa) == len(fb) == 8 * full_blocks, f"a={len(fa)} b={len(fb)} blocks={full_blocks}"))

    # The 40-bit readout pair, reconstructed here exactly as the firmware reconstructs it.
    accx8, accx_full = accx_total(d["sad_accx"]), accx_total(d["sad_full_accx"])
    out.append(("ex10 RUR.ACCX_0/1 reconstruct the SAD the Python reference computes (8-bit samples)",
                accx8 == ref8 == int(d["sad_total"]),
                f"accx pair {accx8}, reference {ref8}, firmware {d['sad_total']}"))
    out.append(("ex10 in the 8-bit sample domain the kernel equals the textbook SAD",
                ref8 == plain8 and int(d["sad_plain_textbook"]) == plain8,
                f"kernel {ref8} vs textbook {plain8} vs firmware {d['sad_plain_textbook']}"))
    out.append(("ex10 the 8-bit sample domain straddles no lane and clamps none",
                straddling(a, b) == 0 and clamped_diff(a, b) == 0
                and int(d["sad_straddling_lanes"]) == 0 and int(d["sad_clamped_lanes"]) == 0,
                f"straddling {straddling(a, b)} (firmware {d['sad_straddling_lanes']}), "
                f"clamped {clamped_diff(a, b)} (firmware {d['sad_clamped_lanes']})"))
    out.append(("ex10 the full-range case follows the instruction rule, not the textbook formula",
                accx_full == ref_full == ref_sad8_rule(fa, fb) == int(d["sad_full_total"]),
                f"accx pair {accx_full}, reference {ref_full}, rule {ref_sad8_rule(fa, fb)}, "
                f"firmware {d['sad_full_total']}"))
    out.append(("ex10 the full-range case is genuinely outside the contract (the two readings differ)",
                ref_full != plain_full and int(d["sad_full_plain_textbook"]) == plain_full,
                f"kernel {ref_full} vs textbook {plain_full}: the two part company by "
                f"{abs(ref_full - plain_full)}"))
    out.append(("ex10 the full-range straddling and clamping lane counts match the Python model",
                straddling(fa, fb) == int(d["sad_full_straddling_lanes"])
                and clamped_diff(fa, fb) == int(d["sad_full_clamped_lanes"])
                and straddling(fa, fb) > 0 and clamped_diff(fa, fb) > 0,
                f"straddling {straddling(fa, fb)} (firmware {d['sad_full_straddling_lanes']}), "
                f"clamped {clamped_diff(fa, fb)} (firmware {d['sad_full_clamped_lanes']})"))

    ha, hb = i16_words(d["halfpel_a"]), i16_words(d["halfpel_b"])
    hout = i16_words(d["halfpel_out"])
    ones = int(d["halfpel_ones8"])
    lanes = int(d["halfpel_lanes"])
    seq = ref_halfpel(ha, hb, ones)
    out.append((f"ex10 half-pel on 8-bit samples: the kernel, the op sequence and the expression all agree "
                f"(ones8={ones})",
                len(hout) == lanes and hout == seq == ref_halfpel_formula(ha, hb),
                f"{lanes} lanes; kernel {hout[:3]} vs sequence {seq[:3]} vs expression "
                f"{ref_halfpel_formula(ha, hb)[:3]}"))

    fha, fhb = i16_words(d["halfpel_full_a"]), i16_words(d["halfpel_full_b"])
    fhout = i16_words(d["halfpel_full_out"])
    model = ref_halfpel(fha, fhb, ones)
    expression = ref_halfpel_formula(fha, fhb)
    sat_pos, sat_neg = halfpel_saturating_lanes(fha, fhb)
    diverging = [i for i in range(len(model)) if model[i] != expression[i]]
    saturating = set([i for i in range(len(fha)) if fha[i] + fhb[i] + 1 > 32767]
                     + [i for i in range(len(fha)) if fha[i] + fhb[i] + 1 < -32768])
    out.append(("ex10 half-pel on full-range lanes follows the kernel's op sequence",
                fhout == model and len(fhout) == int(d["halfpel_full_lanes"]),
                f"{len(fhout)} lanes; first divergence from the model: "
                f"{next((i for i in range(len(model)) if model[i] != fhout[i]), None)}"))
    out.append(("ex10 the diverging lanes are exactly the lanes where the rounding add saturates",
                set(diverging) == saturating
                and len(diverging) == int(d["halfpel_full_mismatch"])
                and sat_pos == int(d["halfpel_full_sat_pos"]) and sat_neg == int(d["halfpel_full_sat_neg"]),
                f"{len(diverging)} diverging (firmware {d['halfpel_full_mismatch']}), saturation set size "
                f"{len(saturating)}, +{sat_pos}/-{sat_neg} (firmware +{d['halfpel_full_sat_pos']}/"
                f"-{d['halfpel_full_sat_neg']})"))
    return out


def ref_block8x8(coef: list[int], block: list[int], shift: int) -> list[int]:
    """out[k][j] = sat16((sum over i of coef[k][i] * block[i][j]) >> shift), with each lane's 40-bit
    accumulator clamped per MAC as EE.VSMULAS.S16.QACC documents."""
    out = []
    for k in range(8):
        for j in range(8):
            acc = 0
            for i in range(8):
                acc = sat40(acc + coef[k * 8 + i] * block[i * 8 + j])
            out.append(sat16(acc >> shift))
    return out


def block8x8_row_sums(coef: list[int], block: list[int]) -> list[list[int]]:
    return [[sum(coef[k * 8 + i] * block[i * 8 + j] for i in range(8)) for j in range(8)] for k in range(8)]


def check_ex11(sec: dict) -> list[tuple[str, bool, str]]:
    """The 8x8 block transform, re-derived from the DATA lines as an int64 matrix product with the
    saturating 16-bit readout."""
    d = sec["data"]
    need = {"coef", "coef_transposed", "block", "out", "out_transposed", "out_saturating", "shift",
            "shift_saturating", "saturated_lanes", "rows_over_40bit", "max_abs_row_sum"}
    missing = sorted(need - set(d))
    if missing:
        return [("ex11 carries the block, the coefficient tables and the readouts", False,
                 f"missing {missing}")]
    out: list[tuple[str, bool, str]] = []
    coef, block = i16_words(d["coef"]), i16_words(d["block"])
    coeft = i16_words(d["coef_transposed"])
    shift, shift_sat = int(d["shift"]), int(d["shift_saturating"])
    if len(coef) != 64 or len(block) != 64 or len(coeft) != 64:
        return [("ex11 carries the 8x8 tables (64 int16 each)", False,
                 f"coef={len(coef)} block={len(block)} coef_transposed={len(coeft)}")]

    got, ref = i16_words(d["out"]), ref_block8x8(coef, block, shift)
    bad = [i for i in range(min(len(got), len(ref))) if got[i] != ref[i]]
    out.append((f"ex11 all 64 coefficients match the Python int64 reference (shift={shift})",
                got == ref, f"{len(bad)} differ, first {bad[:3]}" if bad else "64/64"))
    gott, reft = i16_words(d["out_transposed"]), ref_block8x8(coeft, block, shift)
    out.append(("ex11 the transposed coefficient table transforms the other axis",
                gott == reft, "64/64" if gott == reft else f"first few {gott[:3]} vs {reft[:3]}"))
    out.append(("ex11 the second table printed really is the transpose of the first",
                coeft == [coef[i * 8 + k] for k in range(8) for i in range(8)],
                "coef_t[k][i] == coef[i][k] for all 64"))
    gots, refs = i16_words(d["out_saturating"]), ref_block8x8(coef, block, shift_sat)
    sat_now = sum(1 for v in gots if v in (32767, -32768))
    out.append((f"ex11 the small shift makes EE.SRCMB.S16.QACC saturate, exactly like sat16 (shift={shift_sat})",
                gots == refs and sat_now == int(d["saturated_lanes"]) and sat_now > 0,
                f"{sat_now} clipped lanes (firmware {d['saturated_lanes']})"))
    rows = block8x8_row_sums(coef, block)
    biggest = max(abs(v) for row in rows for v in row)
    over = sum(1 for row in rows for v in row if v > (1 << 39) - 1)
    out.append(("ex11 no output row reaches the 40-bit accumulator's clamp",
                over == 0 and int(d["rows_over_40bit"]) == 0 and biggest == int(d["max_abs_row_sum"]),
                f"largest |row sum| {biggest} (firmware {d['max_abs_row_sum']}), clamp at 2^39-1 = "
                f"{(1 << 39) - 1}, headroom factor {(1 << 39) // max(1, biggest)}"))
    return out


# ------------------------------------------------------------------ ex12: physics and collision

def ref_integrate(pos: list[int], vel: list[int], acc: list[int], lo: int, hi: int) -> list[int]:
    """The kernel's order: VADDS.S32, VADDS.S32, VMIN.S32 (hi), VMAX.S32 (lo)."""
    out = []
    for i in range(min(len(pos), len(vel), len(acc))):
        t = sat32(pos[i] + vel[i])
        u = sat32(t + acc[i])
        out.append(max(min(u, hi), lo))
    return out


def ref_integrate_exact(pos: list[int], vel: list[int], acc: list[int], lo: int, hi: int) -> list[int]:
    return [max(min(pos[i] + vel[i] + acc[i], hi), lo) for i in range(len(pos))]


def ref_sat_masks(boxes: list[int], query: list[int], n_boxes: int) -> list[int]:
    """The plane-major AABB image: box b, axis ax has min at int16 index 48*(b//8) + 16*ax + (b%8) and max
    8 words on; separated iff box_max < query_min or box_min > query_max; -1 for overlap, 0 otherwise."""
    out = []
    for b in range(n_boxes):
        sep = False
        for ax in range(3):
            bmin = boxes[48 * (b // 8) + 16 * ax + (b % 8)]
            bmax = boxes[48 * (b // 8) + 16 * ax + 8 + (b % 8)]
            if bmax < query[2 * ax] or bmin > query[2 * ax + 1]:
                sep = True
        out.append(0 if sep else -1)
    return out


def dist2_lane(pt_a: list[int], pt_b: list[int], i: int) -> int:
    """The 40-bit QACC lane after the three saturating 16-bit subtractions and the three squaring MACs."""
    s = 0
    for ax in range(3):
        k = 24 * (i // 8) + 8 * ax + (i % 8)
        d = sat16(pt_a[k] - pt_b[k])
        s += d * d
    return s


def ref_dist2(pt_a: list[int], pt_b: list[int], n_points: int) -> list[int]:
    return [min(dist2_lane(pt_a, pt_b, i), 0x7FFFFFFF) for i in range(n_points)]


def ref_dist2_q16(pt_a: list[int], pt_b: list[int], n_points: int, shift: int) -> list[int]:
    return [sat16(dist2_lane(pt_a, pt_b, i) >> shift) for i in range(n_points & ~7)]


def dist2_stats(pt_a: list[int], pt_b: list[int], n_points: int) -> dict:
    """The counters the firmware prints, recomputed: axis differences that saturate, points where the
    saturating difference changes the value, points the int32 clamp fires on, and the largest |a-b|."""
    sat_axis = changed = clamps = max_delta = 0
    for i in range(n_points):
        sat_sum = exact = 0
        for ax in range(3):
            k = 24 * (i // 8) + 8 * ax + (i % 8)
            d = pt_a[k] - pt_b[k]
            max_delta = max(max_delta, abs(d))
            if d > 32767 or d < -32768:
                sat_axis += 1
            ds = sat16(d)
            sat_sum += ds * ds
            exact += d * d
        clamps += 1 if sat_sum > 0x7FFFFFFF else 0
        changed += 1 if sat_sum != exact else 0
    return {"saturating_axis_diffs": sat_axis, "changed_by_saturation": changed, "int32_clamps": clamps,
            "max_abs_delta": max_delta}


def check_ex12(sec: dict) -> list[tuple[str, bool, str]]:
    """The three physics/collision kernels, re-derived from the DATA lines."""
    d = sec["data"]
    need = {"pos", "vel", "acc", "integrated", "integrated_narrow_bounds", "objects", "integrate_lo",
            "integrate_hi", "integrate_lo_narrow", "integrate_hi_narrow", "integrate_diverging_from_exact",
            "integrate_moved_by_the_bounds", "boxes", "query", "masks", "n_boxes", "overlap_boxes",
            "pt_a", "pt_b", "dist2", "n_points", "dist2_int32_clamps", "dist2_saturating_axis_diffs",
            "dist2_max_abs_delta", "dist2_changed_by_the_saturating_difference", "dist2_q16", "q16_shift",
            "q16_written", "q16_saturated_lanes"}
    missing = sorted(need - set(d))
    if missing:
        return [("ex12 carries the physics/collision inputs and results", False, f"missing {missing}")]
    out: list[tuple[str, bool, str]] = []

    pos, vel, acc = hex_words(d["pos"]), hex_words(d["vel"]), hex_words(d["acc"])
    got = hex_words(d["integrated"])
    lo, hi = int(d["integrate_lo"]), int(d["integrate_hi"])
    lo_n, hi_n = int(d["integrate_lo_narrow"]), int(d["integrate_hi_narrow"])
    n_obj = int(d["objects"])
    n_boxes, n_points = int(d["n_boxes"]), int(d["n_points"])
    boxes, query, masks = i16_words(d["boxes"]), i16_words(d["query"]), i16_words(d["masks"])
    pt_a, pt_b, d2 = i16_words(d["pt_a"]), i16_words(d["pt_b"]), hex_words(d["dist2"])
    q16, written = i16_words(d["dist2_q16"]), int(d["q16_written"])
    # Structural check first: a DATA line that does not carry what its count says is a failure, not a crash.
    bad_shapes = []
    for what, have, want in (("pos", len(pos), n_obj), ("vel", len(vel), n_obj), ("acc", len(acc), n_obj),
                             ("integrated", len(got), n_obj),
                             ("boxes", len(boxes), 48 * ((n_boxes + 7) // 8)),
                             ("query", len(query), 6), ("masks", len(masks), n_boxes),
                             ("pt_a", len(pt_a), 24 * ((n_points + 7) // 8)),
                             ("pt_b", len(pt_b), 24 * ((n_points + 7) // 8)),
                             ("dist2", len(d2), n_points), ("dist2_q16", len(q16), written)):
        if have != want:
            bad_shapes.append(f"{what}: {have} values, the log says {want}")
    if bad_shapes:
        return [("ex12 prints as many values as its own counts claim", False, "; ".join(bad_shapes))]

    ref = ref_integrate(pos, vel, acc, lo, hi)
    bad = [i for i in range(min(len(got), len(ref))) if got[i] != ref[i]]
    out.append((f"ex12 the {n_obj} integrated positions match the Python saturating-add reference",
                len(got) == n_obj and got == ref, f"{len(bad)} differ, first {bad[:3]}" if bad else
                f"{n_obj}/{n_obj}"))
    exact = ref_integrate_exact(pos, vel, acc, lo, hi)
    src = [i for i in range(n_obj) if -0x80000000 <= pos[i] + vel[i] <= 0x7FFFFFFF]
    in_domain_bad = [i for i in src if got[i] != exact[i]]
    out.append(("ex12 every lane whose pos+vel stays inside int32 equals the exact 64-bit sum",
                not in_domain_bad,
                f"{len(src)} in-domain lanes, {len(in_domain_bad)} disagree "
                f"(firmware counts {d['integrate_diverging_from_exact']} lanes off the exact sum in all)"))
    diverging = [i for i in range(n_obj) if got[i] != exact[i]]
    out.append(("ex12 the lanes that leave the exact sum are exactly the out-of-int32 ones",
                set(diverging) == set(range(n_obj)) - set(src)
                and len(diverging) == int(d["integrate_diverging_from_exact"]),
                f"diverging {diverging} (firmware {d['integrate_diverging_from_exact']}), "
                f"out-of-range {sorted(set(range(n_obj)) - set(src))}"))
    got_n = hex_words(d["integrated_narrow_bounds"])
    ref_n = ref_integrate(pos, vel, acc, lo_n, hi_n)
    moved = sum(1 for i in range(n_obj) if got_n[i] != got[i])
    out.append((f"ex12 the same lanes under the [{lo_n}, {hi_n}] bounds match the reference",
                got_n == ref_n, f"{len([i for i in range(n_obj) if got_n[i] != ref_n[i]])} differ"))
    out.append(("ex12 the narrow bounds move the lanes the clamp is supposed to move",
                moved == int(d["integrate_moved_by_the_bounds"]) and moved > 0,
                f"{moved} lanes moved (firmware {d['integrate_moved_by_the_bounds']})"))

    ref_m = ref_sat_masks(boxes, query, n_boxes)
    out.append((f"ex12 all {n_boxes} AABB masks match the Python separating-axis reference",
                masks == ref_m, f"got {masks[:8]}... want {ref_m[:8]}..." if masks != ref_m else
                f"{n_boxes}/{n_boxes}"))
    overlaps = sum(1 for v in ref_m if v == -1)
    out.append(("ex12 the AABB probe reaches both verdicts and its overlap count is the model's",
                overlaps == int(d["overlap_boxes"]) and 0 < overlaps < n_boxes,
                f"{overlaps} overlapping (firmware {d['overlap_boxes']}) of {n_boxes}"))

    ref_d2 = ref_dist2(pt_a, pt_b, n_points)
    bad = [i for i in range(min(len(d2), len(ref_d2))) if d2[i] != ref_d2[i]]
    out.append((f"ex12 all {n_points} squared distances match the Python QACC-lane reference",
                len(d2) == n_points and d2 == ref_d2, f"{len(bad)} differ, first {bad[:3]}" if bad else
                f"{n_points}/{n_points}"))
    st = dist2_stats(pt_a, pt_b, n_points)
    agree = (st["int32_clamps"] == int(d["dist2_int32_clamps"])
             and st["saturating_axis_diffs"] == int(d["dist2_saturating_axis_diffs"])
             and st["max_abs_delta"] == int(d["dist2_max_abs_delta"])
             and st["changed_by_saturation"] == int(d["dist2_changed_by_the_saturating_difference"]))
    out.append(("ex12 the saturating-difference, int32-clamp and max|a-b| counters are the Python model's",
                agree and st["int32_clamps"] > 0 and st["saturating_axis_diffs"] > 0,
                f"clamps {st['int32_clamps']} (firmware {d['dist2_int32_clamps']}), saturating axes "
                f"{st['saturating_axis_diffs']} (firmware {d['dist2_saturating_axis_diffs']}), max|a-b| "
                f"{st['max_abs_delta']} (firmware {d['dist2_max_abs_delta']}), changed "
                f"{st['changed_by_saturation']} (firmware {d['dist2_changed_by_the_saturating_difference']})"))
    # the int32 clamp must preserve the r^2 test, and the low 32 bits must be the whole lane
    lane_ok = all(dist2_lane(pt_a, pt_b, i) <= (1 << 40) - 1 and
                  (dist2_lane(pt_a, pt_b, i) < (1 << 32) or True) for i in range(n_points))
    hi_bits = [i for i in range(n_points) if dist2_lane(pt_a, pt_b, i) >= (1 << 32)]
    out.append(("ex12 no QACC lane needs bits 32..39 (the saturating difference bounds dist2 below 2^32)",
                not hi_bits and lane_ok, f"{len(hi_bits)} lanes at or above 2^32"))

    shift = int(d["q16_shift"])
    ref_q16 = ref_dist2_q16(pt_a, pt_b, n_points, shift)
    sat_q16 = sum(1 for v in ref_q16 if v in (32767, -32768))
    out.append((f"ex12 the Q16 readout (>> {shift}, saturating) matches on every written pair",
                written == (n_points & ~7) and len(q16) == written and q16 == ref_q16,
                f"written {written} (expected {n_points & ~7}), {len([i for i in range(min(len(q16), len(ref_q16))) if q16[i] != ref_q16[i]])} differ"))
    out.append(("ex12 the Q16 readout saturates where the model says it does",
                sat_q16 == int(d["q16_saturated_lanes"]) and sat_q16 > 0,
                f"{sat_q16} clipped lanes (firmware {d['q16_saturated_lanes']})"))
    return out


CHECKS = {"ex01": check_ex01, "ex02": check_ex02, "ex03": check_ex03, "ex04": check_ex04,
          "ex05": check_ex05, "ex06": check_ex06, "ex07": check_ex07, "ex08": check_ex08,
          "ex09": check_ex09, "ex10": check_ex10, "ex11": check_ex11, "ex12": check_ex12}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--json", help="write the report here as JSON")
    a = ap.parse_args()

    sections = parse(a.log)
    if not sections:
        print(f"no EX lines in {a.log}: is this a log from examples/firmware?", file=sys.stderr)
        return 2

    failures, checks = [], []
    bench = sections.get("__bench__", {}).get("bench", {})
    for ex in sorted(sections):
        if ex == "__bench__":
            continue
        fn = CHECKS.get(ex)
        if fn is None:
            continue
        print(f"\n== {ex} ==")
        for label, ok, detail in fn(sections[ex]):
            checks.append({"example": ex, "label": label, "ok": ok, "detail": detail})
            print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail else ""))
            if not ok:
                failures.append(f"{ex}: {label}")
        for res in sections[ex]["results"]:
            print(f"  (firmware self-check: ok={res['ok']} fail={res['fail']})")
            if res["fail"]:
                failures.append(f"{ex}: the firmware's own self-check reported {res['fail']} failures")

    report = {"log": os.path.basename(a.log), "checks": checks, "failures": failures,
              "bench": bench}

    if bench:
        print("\n== performance (single run, cycles per element; the C baseline is built at -O2) ==")
        for kernel, b in sorted(bench.items()):
            per_pie = b["cycles_pie"] / b["elements"]
            per_c = b["cycles_c"] / b["elements"]
            ratio = f"{per_c / per_pie:5.2f}x" if per_pie else "n/a"
            print(f"  {kernel:<12} elements={b['elements']:6d}  pie={per_pie:7.3f} c/elem  "
                  f"c={per_c:7.3f} c/elem  ratio={ratio}")

    if a.json:
        json.dump(report, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(checks) - len(failures)} of {len(checks)} "
          f"checks passed")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
