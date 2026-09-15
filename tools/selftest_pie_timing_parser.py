#!/usr/bin/env python3
"""Self-test for tools/parse_pie_timing.py, using synthetic logs.

No hardware needed: this checks that the parser (a) derives the staging arithmetic correctly from cycle
counts, and (b) refuses to publish derived facts when an anchor does not reproduce. Both properties are what
make a measured result trustworthy, and both are cheaper to test here than on a device.

    .venv/bin/python tools/selftest_pie_timing_parser.py
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "experiments", "pie-timing", "cases.json")

# The truth the synthetic log encodes: interlock at issue distance 1, per case.
TRUTH = {
    "anchor_accx_M_to_E": 1, "anchor_qs_M_to_E": 1, "anchor_qr_E_to_E": 0,
    "native_load_use": 1, "native_alu_use": 0,
    "LD_QR_def_stage": 1, "QR_load_to_QR_op": 1, "MV_QR_def_stage": 1,
    "ST_QR_use_stage": 1, "LD_QR_reads_as": 0,
    # The QACC_H/QACC_L cases: what the model predicts (Table 1.7-2 stages + rule 1.7.1).
    "qacc_HL_vmulas_u16_to_srcmb_s16": 1, "qacc_HL_vmulas_s16_to_srcmb_s16": 1,
    "qacc_HL_vmulas_u16_to_vmulas_u16": 0, "qacc_HL_vmulas_u16_to_srcmb_s16_andq_indep": 1,
    "qacc_L_vmulas_u16_to_stqacc_l": 1,
}

# The one case the "broken prediction" log moves away from its prediction. It is an anchor-free case on
# purpose: a missed prediction must leave `valid` (and the derived stages) alone.
MIS_PREDICTED = "qacc_HL_vmulas_u16_to_srcmb_s16"


def synth_log(path: str, break_accx_anchor: bool = False, rounds: int = 1,
              miss_qacc_prediction: bool = False) -> None:
    """A log in the format the measurement firmware prints.

    From the firmware's side the report is emitted once per host trigger ("ROUND", "BEGIN ... round=N",
    ... "END"), so `rounds > 1` reproduces a capture that asked for the report more than once -- the
    repeats then appear twice and the parser has to merge them rather than trip over the duplicates.

    `miss_qacc_prediction` makes the QACC case cost one cycle more than the model predicts. That is the
    finding a measurement may legitimately produce, and it must leave `valid` and the derived stages
    untouched: the anchors test the method, the prediction tests the manual.
    """
    cases = json.load(open(CASES, encoding="utf-8"))
    iters = int(cases["iterations"])
    random.seed(7)
    lines = ["HOST round_wait=10 ready=yes"]
    for rnd in range(rounds):
        lines += ["ROUND %d" % rnd,
                  "ENV chip=esp32s3 cores=2 revision=0.2 cpu_freq_mhz=240 idf=v6.0.1",
                  f"BEGIN measurements=1 repeats=5 round={rnd}"]
        for c in cases["cases"]:
            stall = TRUTH.get(c["id"], 0)
            D = stall + 1
            for d in c["distances"]:
                for variant in ("dep", "indep"):
                    eff = stall if (variant == "dep" and d < D) else 0
                    cycles = 100_000 + d * 2_000 + eff * iters + random.randint(0, 3)
                    if break_accx_anchor and c["id"] == "anchor_accx_M_to_E" and d == 1 and variant == "indep":
                        cycles += iters                 # removes the interlock the TRM says must be there
                    if miss_qacc_prediction and c["id"] == MIS_PREDICTED and d == 1 and variant == "dep":
                        cycles += iters                 # one cycle more than the model predicts
                    for rep in range(5):
                        lines.append(f"MEAS id={c['id']} d={d} variant={variant} repeat={rep} "
                                     f"cycles={cycles + rep}")
        for a in cases.get("alone", []):
            for rep in range(5):
                lines.append(f"MEAS id={a['id']} d=0 variant=alone repeat={rep} cycles={100_000 + rep}")
        lines.append("END")
    lines.append("DONE")
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def run(log: str, out: str) -> tuple[int, dict]:
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools/parse_pie_timing.py"), log, "--out", out],
                       capture_output=True, text=True)
    return r.returncode, json.load(open(out, encoding="utf-8"))


def main() -> int:
    failed = []
    with tempfile.TemporaryDirectory() as tmp:
        good = os.path.join(tmp, "good.log")
        bad = os.path.join(tmp, "bad.log")
        synth_log(good)
        synth_log(bad, break_accx_anchor=True)

        rc, res = run(good, os.path.join(tmp, "good.json"))
        if rc != 0 or not res["valid"]:
            failed.append("a log whose anchors match the TRM must be VALID")
        stages = {(d["instruction"], d["attribute"]): d["stage"] for d in res["derived"]}
        expected = {("LD.QR", "operand def stage"): 2, ("MV.QR", "operand def stage"): 2,
                    ("ST.QR", "operand use stage"): 1,
                    ("LD.QR", "address operand (as) use stage"): 1}
        if stages != expected:
            failed.append(f"derived stages {stages} != expected {expected}")
        if not all(a["ok"] for a in res["anchors"]):
            failed.append("all synthetic anchors should reproduce")

        # Prediction-versus-measurement: reported for the QACC cases, and a *hit* on a log built from the
        # model's own numbers. The set is checked exactly, so a case silently dropping out is a failure.
        preds = {p["case"]: p for p in res["predictions"]}
        if set(preds) != {c["id"] for c in json.load(open(CASES, encoding="utf-8"))["cases"]
                          if c.get("predict")}:
            failed.append(f"predictions cover {sorted(preds)}, expected every case with a `predict` block")
        elif not all(p["match"] for p in preds.values()):
            failed.append("a log that encodes the predicted stalls must report every prediction as a HIT")
        elif preds[MIS_PREDICTED]["measured_min_issue_distance_D"] != 2:
            failed.append(f"expected D=2 for {MIS_PREDICTED}, got "
                          f"{preds[MIS_PREDICTED]['measured_min_issue_distance_D']}")

        # A missed prediction is a finding about the manual, NOT a broken run: `valid` and the derived
        # stages must survive it untouched (the anchors are what gate validity).
        missed = os.path.join(tmp, "missed.log")
        synth_log(missed, miss_qacc_prediction=True)
        rc, res_miss = run(missed, os.path.join(tmp, "missed.json"))
        mp = {p["case"]: p for p in res_miss["predictions"]}
        if rc != 0 or not res_miss["valid"]:
            failed.append("a missed prediction must not make the run invalid")
        if mp.get(MIS_PREDICTED, {}).get("match") is not False:
            failed.append(f"a QACC pair one cycle off its prediction must be reported as a MISS: "
                          f"{mp.get(MIS_PREDICTED)}")
        if not res_miss["derived"]:
            failed.append("a missed prediction must not suppress the derived stages")
        if res_miss["derived"] != res["derived"]:
            failed.append("a missed prediction changed the derived stages -- it must not touch them")

        rc, res = run(bad, os.path.join(tmp, "bad.json"))
        if rc == 0 or res["valid"]:
            failed.append("a log with a broken anchor must be NOT VALID (exit 1)")
        if res["derived"]:
            failed.append("an invalid run must not publish derived facts")

        # The firmware re-prints its whole report for each host trigger; a capture that asks twice must
        # still parse, and extra rounds must add repeats rather than rows (the noise floor is per row).
        multi = os.path.join(tmp, "multi.log")
        synth_log(multi, rounds=3)
        rc, res = run(multi, os.path.join(tmp, "multi.json"))
        rc1, single = run(good, os.path.join(tmp, "good.json"))
        if rc != 0 or not res["valid"]:
            failed.append("a three-round log must still be VALID")
        elif max(r["noise_cycles"] for r in res["measurements"] if r["distance"] == 1) > 0.5:
            failed.append("duplicate rounds must not inflate the noise floor")
        elif len(res["measurements"]) != len(single["measurements"]):
            failed.append(f"three rounds produced {len(res['measurements'])} rows, one round "
                          f"{len(single['measurements'])}: rounds must add repeats, not rows")
        elif rc1 != 0:
            failed.append("the single-round log stopped parsing")

    if failed:
        for f in failed:
            print(f"FAIL  {f}")
        return 1
    print("PASSED: parser self-test (valid run derives stages; broken anchor publishes nothing; "
          "multi-round logs parse; a missed prediction is reported without invalidating the run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
