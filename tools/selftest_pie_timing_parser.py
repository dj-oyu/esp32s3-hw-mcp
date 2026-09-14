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
}


def synth_log(path: str, break_accx_anchor: bool = False, rounds: int = 1) -> None:
    """A log in the format the measurement firmware prints.

    From the firmware's side the report is emitted once per host trigger ("ROUND", "BEGIN ... round=N",
    ... "END"), so `rounds > 1` reproduces a capture that asked for the report more than once -- the
    repeats then appear twice and the parser has to merge them rather than trip over the duplicates.
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
          "multi-round logs parse)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
