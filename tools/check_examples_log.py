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
    ref = []
    for r in range(4):
        for j in range(8):
            acc = 0
            for k in range(4):
                acc += m[r * 4 + k] * v[k * 8 + j]
                acc = max(-(1 << 39), min((1 << 39) - 1, acc))
            ref.append(as_i16(acc >> 16))
    bad = [(i, got[i], ref[i]) for i in range(len(got)) if got[i] != ref[i]]
    return [("ex07 all 32 transformed coordinates match the Python model", not bad,
             f"{len(bad)} mismatches, first {bad[:3]}" if bad else "32/32")]


def check_ex08(sec: dict) -> list[tuple[str, bool, str]]:
    """The framebuffer effects, re-derived from the pixel arrays the log carries."""
    d = sec["data"]
    if not {"a", "b"} <= set(d):
        return [("ex08 carries its pixel rows", False, "missing a/b")]
    a, b = i16_words(d["a"]), i16_words(d["b"])
    out = []
    if "half_blend" in d:
        got = i16_words(d["half_blend"])
        ref = [as_i16(((x & 0xF7DE) >> 1) + ((y & 0xF7DE) >> 1)) for x, y in zip(a, b)]
        bad = [(i, got[i], ref[i]) for i in range(len(got)) if got[i] != ref[i]]
        out.append(("ex08 half_blend (mask-shift-add) matches the Python reference", not bad,
                    f"{len(bad)} mismatches, first {bad[:3]}" if bad else f"{len(got)}/{len(got)}"))
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
    if "tint32" in d:
        got = hex_words(d["tint32"])
        ref = [as_i32((x * 300) >> 8) for x in a]
        bad = [(i, got[i], ref[i]) for i in range(len(got)) if got[i] != ref[i]]
        out.append(("ex08 tint (per-lane multiply with a SAR shift) matches the reference", not bad,
                    f"{len(bad)} mismatches, first {bad[:3]}" if bad else f"{len(got)}/{len(got)}"))
    return out


CHECKS = {"ex01": check_ex01, "ex02": check_ex02, "ex03": check_ex03, "ex04": check_ex04,
          "ex05": check_ex05, "ex06": check_ex06, "ex07": check_ex07, "ex08": check_ex08}


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
            print(f"  {kernel:<12} elements={b['elements']:6d}  pie={per_pie:7.3f} c/elem  "
                  f"c={per_c:7.3f} c/elem  ratio={per_c / per_pie:5.2f}x")

    if a.json:
        json.dump(report, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(checks) - len(failures)} of {len(checks)} "
          f"checks passed")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
