#!/usr/bin/env python3
"""Self-test for tools/asm_toolchain.py -- the bridge that checks the manual's encodings against binutils.

The value of that bridge is that it can *disagree* with the manual, so this test pins both directions:
encodings that must match, the two instructions whose printed diagrams are known to be short or mislabelled
(MV.QR, ST.QR), and the failure paths (a wrong expectation, an unassemblable line, a missing toolchain).
If the extraction ever "fixes" MV.QR by inventing the missing bit, this fails -- which is the point: the
diagram is wrong in the manual, and the tool is supposed to say so rather than paper over it.

    .venv/bin/python tools/selftest_asm_toolchain.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asm_toolchain as at  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED: list[str] = []
CHECKED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKED
    CHECKED += 1
    if ok:
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} {detail}")


def main() -> int:
    info = at.toolchain_info()
    if not info["available"]:
        print(f"SKIPPED: no Espressif assembler found ({info.get('hint')})")
        return 0
    print(f"toolchain: {info['assembler']}\n           {info['version']}")

    # 1. What the assembler encodes for known lines. Values come from objdump on the same toolchain.
    r = at.assemble("ld.qr q0, a3, 0\n    ee.andq q3, q0, q1")
    check("assemble accepts a PIE snippet", r["accepted"], str(r.get("stderr"))[:200])
    got = [(i["source"], i["word"]) for i in r.get("body_instructions", [])]
    check("two instruction words, in source order",
          got == [("ld.qr q0, a3, 0", "cd2034"), ("ee.andq q3, q0, q1", "ddb024")], str(got))

    # 2. Round trip: word -> mnemonic must come back the same, and byte order must not be reversed.
    d = at.decode("cd2034")
    check("decode(cd2034) is ld.qr q0, a3, 0", (d.get("disassembly") or "").replace("\t", " ") ==
          "ld.qr q0, a3, 0", json.dumps(d))
    check("memory-order bytes are the reverse of the printed word", d.get("bytes_memory") == "3420cd",
          json.dumps(d))

    # 3. Rejection paths must be reported, not raised.
    bad = at.assemble("bogus.insn a1, a2")
    check("an unknown mnemonic is rejected with the assembler's message",
          bad["accepted"] is False and any("unknown opcode" in e for e in bad.get("errors", [])),
          json.dumps(bad.get("errors"))[:160])
    missing = at.assemble("ld.qr q0, a3, 0", raw=False)
    saved, at.find_tool = at.find_tool, lambda name: None
    try:
        gone = at.assemble("ld.qr q0, a3, 0")
        gone_check = at.check_instruction({"name": "EE.ANDQ", "assembler_syntax": "EE.ANDQ qa, qx, qy",
                                           "instruction_word": "11\nqa[2:1]\n1101\nqa[0]\n011\nqy[2:1]\n00\n"
                                                               "qx[2:1]\nqy[0]\nqx[0]\n0100"})
    finally:
        at.find_tool = saved
    check("a missing toolchain is reported, not crashed on",
          missing["accepted"] and gone["error"] == "toolchain_missing"
          and gone_check["status"] == "toolchain_missing", json.dumps(gone)[:160])

    # 4. The manual's diagrams against the toolchain.
    table = {i["name"]: i for i in json.load(open(os.path.join(ROOT, "data", "pie_instructions.json"),
                                                  encoding="utf-8"))}
    andq = at.check_instruction(table["EE.ANDQ"])
    check("EE.ANDQ: the printed diagram reproduces the assembler's word for two operand sets",
          andq["status"] == "match" and len(andq["comparisons"]) == 2
          and all(c["manual_word"] == c["toolchain_word"] for c in andq["comparisons"]),
          json.dumps(andq)[:200])

    ldqr = at.check_instruction(table["LD.QR"])
    check("LD.QR: the immediate's step (16) and its sign are what the assembler uses "
          "(-128 -> 0b1000 in imm[3:0])",
          ldqr["status"] == "match" and all(c["match"] for c in ldqr["comparisons"]),
          json.dumps(ldqr.get("comparisons"))[:240])

    mvqr = at.check_instruction(table["MV.QR"])
    check("MV.QR: the printed diagram is one bit short of the 24-bit instruction, and is reported as a "
          "mismatch rather than silently padded",
          mvqr["status"] == "mismatch"
          and all(c["bit_width_manual"] == 23 and c["bit_width_toolchain"] == 24
                  for c in mvqr["comparisons"]),
          json.dumps(mvqr.get("comparisons"))[:200])
    # The missing bit sits inside the constant printed as `000` between qs[2:1] and qs[0]; inserting a 0
    # there must reproduce the assembler's word exactly. This is what pins *where* the manual is short.
    fields, _ = at.parse_layout(["10", "qu[2:1]", "1111", "qu[0]", "000", "qs[2:1]", "0000", "qs[0]",
                                 "00100"])
    patched = at.encode_from_manual(fields, {"qu": 5, "qs": 5})["word"]
    check("MV.QR with that one bit added matches the assembler (af8824)", patched == "af8824", patched)

    stqr = at.check_instruction(table["ST.QR"])
    check("ST.QR: the syntax line names LD.QR (manual typo) and is reported as such",
          stqr["status"] == "syntax_names_other_instruction" and stqr["syntax_mnemonic"] == "LD.QR",
          json.dumps(stqr)[:160])

    ldaccx = at.check_instruction(table["EE.LD.ACCX.IP"])
    check("EE.LD.ACCX.IP: a bare range in the syntax ('as, -1024..1016') is an operand, not a range for "
          "the address register",
          ldaccx["status"] == "match", json.dumps(ldaccx)[:200])

    ldf = at.check_instruction(table["EE.LDF.128.IP"])
    check("EE.LDF.128.IP: four distinct f registers are synthesised (the assembler rejects repeats)",
          ldf["status"] == "match", json.dumps(ldf)[:200])

    # 5. The recorded errata must still reproduce. Committed in data/pie_encoding_errata.json, which the MCP
    #    serves; if the extraction is ever "fixed" so one of these silently starts matching, this fails and
    #    the file has to be regenerated deliberately.
    errata_path = os.path.join(ROOT, "data", "pie_encoding_errata.json")
    errata = json.load(open(errata_path, encoding="utf-8"))
    recorded = {e["instruction"]: e["status"] for e in errata["instructions"]}
    check("errata records exactly the six disagreements (fails if one silently disappears)",
          recorded == {"EE.SRC.Q.LD.IP": "layout_not_machine_readable",
                       "EE.VMULAS.S8.QACC.LD.IP": "layout_not_machine_readable",
                       "EE.ST.ACCX.IP": "assembler_rejected",
                       "EE.VLDBC.32.IP": "mismatch",
                       "MV.QR": "mismatch",
                       "ST.QR": "syntax_names_other_instruction"}, json.dumps(recorded))
    for name, want in recorded.items():
        check(f"errata entry {name} still reproduces as {want}",
              at.check_instruction(table[name])["status"] == want)

    # 6. The immediate's documented range is part of the encoding, and for these two the manual's printed
    #    range does not agree with what the assembler accepts. Pinned because firmware must follow the
    #    assembler: a displacement the manual calls legal is refused.
    def immediate_scale(fmt: str) -> tuple[int, int, int]:
        accepted = [v for v in range(-1056, 1057, 4) if at.assemble(fmt.format(v))["accepted"]]
        steps = {accepted[i + 1] - accepted[i] for i in range(len(accepted) - 1)}
        return min(accepted), max(accepted), sorted(steps)[0]

    lo, hi, step = immediate_scale("EE.ST.ACCX.IP a5, {}")
    check("EE.ST.ACCX.IP: the assembler takes -1024..1016 in steps of 8 (the manual prints -512..508)",
          (lo, hi, step) == (-1024, 1016, 8), f"{lo}..{hi} step {step}")
    lo, hi, step = immediate_scale("EE.VLDBC.32.IP q5, a5, {}")
    check("EE.VLDBC.32.IP: the assembler takes -512..508 in steps of 4 (the manual prints -256..252)",
          (lo, hi, step) == (-512, 508, 4), f"{lo}..{hi} step {step}")
    lo, hi, step = immediate_scale("LD.QR q5, a5, {}")
    check("LD.QR: assembler agrees with the manual (-128..112 in steps of 16)",
          (lo, hi, step) == (-128, 112, 16), f"{lo}..{hi} step {step}")

    print(f"\n{'FAILED' if FAILED else 'PASSED'}: {len(FAILED)} failure(s) of {CHECKED} checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
