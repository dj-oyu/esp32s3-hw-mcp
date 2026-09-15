#!/usr/bin/env python3
"""Turn a captured PIE timing log into measured facts, and check the method against the TRM's own table.

The log comes from experiments/pie-timing (see notes/04-hardware-experiment.md). Interpreting it is the part
that can quietly go wrong, so every number in the output states its basis:

  stall_per_iteration = (min_cycles_dep - min_cycles_indep) / iterations

using the minimum over repeats (the least-disturbed run), and reporting the spread so a claim of "0 stalls"
is only accepted when the noise floor is well below one cycle.

The harness's own validity gate: cases whose expected stall the TRM states (a stage-2 producer read by a
stage-1 consumer, and two zero-stall controls) must reproduce. If they do not, `valid` is false and the
derived QR results are reported as unusable rather than guessed.

Cases may also carry a `predict` block: the model's expectation for a pair whose stages Table 1.7-2 *does*
state, but which silicon has never checked. Those are reported under `predictions` as prediction-versus-
measurement, and they never enter the validity gate -- a miss there is a finding about the manual, not
evidence that the method is broken (see `predict_note` in cases.json).

    .venv/bin/python tools/parse_pie_timing.py <log> [--out data/pie_timing_measured.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINE_RE = re.compile(r"^MEAS id=(\S+) d=(\d+) variant=(dep|indep) repeat=(\d+) cycles=(\d+)\s*$")
ENV_RE = re.compile(r"^ENV (.*)$")


def parse(path: str) -> tuple[dict, dict, int]:
    env: dict[str, str] = {}
    runs: dict[tuple[str, int, str], list[int]] = {}
    iterations = None
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        m = ENV_RE.match(line)
        if m:
            for kv in m.group(1).split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    env[k] = v
            continue
        m = re.match(r"^BEGIN measurements=(\d+) repeats=(\d+)", line)
        if m:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        cid, d, variant, _rep, cycles = m.group(1), int(m.group(2)), m.group(3), int(m.group(4)), int(m.group(5))
        runs.setdefault((cid, d, variant), []).append(cycles)

    cases = json.load(open(os.path.join(ROOT, "experiments/pie-timing/cases.json"), encoding="utf-8"))
    iterations = int(cases["iterations"])
    return env, runs, iterations


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--out", default=os.path.join(ROOT, "data/pie_timing_measured.json"))
    ap.add_argument("--firmware-rev", default="", help="git revision of the flashed firmware, for provenance")
    ap.add_argument("--app-image-sha256", default="",
                    help="sha256 of the application image that was flashed; ties the log to exact bytes")
    a = ap.parse_args()

    env, runs, iterations = parse(a.log)
    cases = json.load(open(os.path.join(ROOT, "experiments/pie-timing/cases.json"), encoding="utf-8"))
    case_by_id = {c["id"]: c for c in cases["cases"]}

    rows, anchors, problems = [], [], []
    for cid, case in case_by_id.items():
        for d in case["distances"]:
            dep = runs.get((cid, d, "dep"))
            indep = runs.get((cid, d, "indep"))
            if not dep or not indep:
                problems.append(f"{cid} d={d}: missing dep/indep runs in the log")
                continue
            min_dep, min_indep = min(dep), min(indep)
            spread = max(max(dep) - min_dep, max(indep) - min_indep)
            stall = (min_dep - min_indep) / iterations
            rows.append({"case": cid, "distance": d, "stall_cycles": round(stall, 4),
                         "noise_cycles": round(spread / iterations, 4),
                         "min_cycles_dep": min_dep, "min_cycles_indep": min_indep})
            if d == 1 and case.get("expect"):
                ok = abs(stall - case["expect"]["stall_cycles"]) < 0.25
                anchors.append({"case": cid, "expected_stall": case["expect"]["stall_cycles"],
                                "measured_stall": round(stall, 4), "ok": ok,
                                "why": case["expect"]["why"], "noise_cycles": round(spread / iterations, 4)})

    # A "0 stall" claim needs the noise floor to be well under a cycle, and every anchor to reproduce.
    anchors_ok = bool(anchors) and all(x["ok"] for x in anchors)
    noisy = [r for r in rows if r["distance"] == 1 and r["noise_cycles"] > 0.5]
    valid = anchors_ok and not noisy and not problems

    def stall_at(cid: str, d: int) -> float | None:
        return next((r["stall_cycles"] for r in rows if r["case"] == cid and r["distance"] == d), None)

    def distance_zero(cid: str) -> int | None:
        for r in sorted([x for x in rows if x["case"] == cid], key=lambda x: x["distance"]):
            if r["stall_cycles"] < 0.25:
                return r["distance"]
        return None

    derived = []
    # TRM Table 1.7-2 gives the consumer side of these cases, so the producer's stage follows from the stall.
    if valid:
        s = stall_at("LD_QR_def_stage", 1)
        if s is not None:
            derived.append({"instruction": "LD.QR", "attribute": "operand def stage", "stage": 1 + round(s),
                            "basis": "consumer EE.ANDQ reads its operands at stage 1 (E) per Table 1.7-2, "
                                     f"measured interlock {s} cycles at issue distance 1",
                            "citation": "measured; TRM p301 documents the instruction but Table 1.7-2 omits it"})
        s = stall_at("MV_QR_def_stage", 1)
        if s is not None:
            derived.append({"instruction": "MV.QR", "attribute": "operand def stage", "stage": 1 + round(s),
                            "basis": "consumer EE.ANDQ reads its operands at stage 1 (E) per Table 1.7-2, "
                                     f"measured interlock {s} cycles at issue distance 1",
                            "citation": "measured; TRM p303 documents the instruction but Table 1.7-2 omits it"})
        s = stall_at("ST_QR_use_stage", 1)
        ld_stage = next((d["stage"] for d in derived if d["instruction"] == "LD.QR"), None)
        if s is not None and ld_stage is not None:
            derived.append({"instruction": "ST.QR", "attribute": "operand use stage", "stage": ld_stage - round(s),
                            "basis": f"producer LD.QR writes at stage {ld_stage} (measured), measured interlock "
                                     f"{s} cycles at issue distance 1",
                            "citation": "measured; TRM p302 documents the instruction but Table 1.7-2 omits it"})
        s = stall_at("LD_QR_reads_as", 1)
        if s is not None:
            derived.append({"instruction": "LD.QR", "attribute": "address operand (as) use stage",
                            "stage": 1 - round(s),
                            "basis": "the producer EE.LD.128.USAR.IP writes its address register at stage 1 per "
                                     f"Table 1.7-2; measured interlock {s} cycles",
                            "citation": "measured"})

    # Prediction-versus-measurement for the pairs whose staging the table states but silicon had never
    # checked. Kept out of `valid` on purpose: the anchors test the *method*, this tests the *manual*.
    predictions = []
    for cid, case in case_by_id.items():
        p = case.get("predict")
        if not p:
            continue
        case_rows = sorted([r for r in rows if r["case"] == cid], key=lambda r: r["distance"])
        at_d1 = next((r for r in case_rows if r["distance"] == 1), None)
        if at_d1 is None:
            continue
        measured = at_d1["stall_cycles"]
        predictions.append({
            "case": cid,
            "producer": case["producer"],
            "consumer": case["consumer_dep"],
            "registers": list(p.get("registers", [])),
            "predicted_stall_at_d1": p["stall_cycles"],
            "measured_stall_at_d1": measured,
            "predicted_min_issue_distance_D": int(p["stall_cycles"]) + 1,
            "measured_min_issue_distance_D": distance_zero(cid),
            "distances_measured": sorted(r["distance"] for r in case_rows),
            "match": abs(measured - p["stall_cycles"]) < 0.25,
            "stall_by_distance": {str(r["distance"]): r["stall_cycles"] for r in case_rows},
            "noise_cycles_at_d1": at_d1["noise_cycles"],
            "min_cycles_dep": at_d1["min_cycles_dep"], "min_cycles_indep": at_d1["min_cycles_indep"],
            "why": p["why"],
            "basis": f"measured stall (min_cycles_dep - min_cycles_indep) / {iterations} at issue "
                     f"distance 1, dep={at_d1['min_cycles_dep']} indep={at_d1['min_cycles_indep']}; the same "
                     f"row feeds `measurements`",
            **({"confound": case["predict_confound"]} if case.get("predict_confound") else {}),
        })

    out = {
        "provenance": {
            "kind": "measured_on_hardware",
            "note": "These values come from a run on silicon, not from a document. Every entry keeps the log "
                    "line basis in `basis`, and the harness anchors are listed so a reader can see whether the "
                    "method reproduced the manual's own numbers.",
            "log": os.path.basename(a.log),
            "firmware_git_rev": a.firmware_rev or None,
            "firmware_image_sha256": a.app_image_sha256 or None,
            "iterations_per_measurement": iterations,
            "environment": env,
        },
        "valid": valid,
        "anchors": anchors,
        "measurements": rows,
        "derived": derived,
        "predictions": predictions,
        "problems": problems,
        "caveats": [
            "Adjacent-pair interlocks only; hardware-resource (1.7.2) and control (1.7.3) hazards are untouched.",
            "Stall counts are quoted in whole cycles: the reported noise floor bounds the resolution.",
            "A stage derived from a stall difference assumes the rule of TRM 1.7.1 holds on this silicon; the "
            "anchors are what test that assumption.",
            "`predictions` compares the model against the same raw rows as `measurements`, and deliberately does "
            "not gate `valid`: a miss there says the manual's table does not match this silicon, which is the "
            "finding, not a broken run.",
        ],
    }
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{'VALID' if valid else 'NOT VALID'}: {len(rows)} measurements, {len(anchors)} anchors, "
          f"{len(derived)} derived facts -> {os.path.relpath(a.out, ROOT)}")
    for x in anchors:
        print(f"  anchor {x['case']:24s} expected {x['expected_stall']} measured {x['measured_stall']:.3f} "
              f"noise {x['noise_cycles']:.3f} {'ok' if x['ok'] else 'FAIL'}")
    for d in derived:
        print(f"  derived {d['instruction']:8s} {d['attribute']:26s} -> stage {d['stage']}")
    for p in predictions:
        print(f"  predict {p['case']:40s} predicted {p['predicted_stall_at_d1']} measured "
              f"{p['measured_stall_at_d1']:.3f} (D predicted {p['predicted_min_issue_distance_D']}, "
              f"measured {p['measured_min_issue_distance_D']}) "
              f"{'HIT' if p['match'] else 'MISS'}")
    if problems:
        for p in problems:
            print(f"  problem {p}")
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
