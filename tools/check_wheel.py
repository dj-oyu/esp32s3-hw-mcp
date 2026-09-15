#!/usr/bin/env python3
"""Gate the packaged build: what the registry says ships must actually be inside the wheel.

    uv build                       # or: python -m build --wheel
    .venv/bin/python tools/check_wheel.py dist/esp32s3_hw_mcp-<version>-py3-none-any.whl

Why this exists. Every tool answers from data files, and a wheel that loses one still starts, still lists
its tools, and answers from nothing -- a failure nobody notices until a question is asked, on someone else's
machine, under `uvx`. So the wheel is checked against the registry's own declaration rather than a hand-kept
list, and against the checkout byte for byte, so a wheel built from an older commit fails here:

  * every artifact the registry marks shipped is in the wheel, and its bytes equal the checkout's;
  * the two scripts the server itself runs (the assembler bridge, the corpus builder) and NOTICE.md ship;
  * the counts inside the wheel's own copies are the ones the README states (1581 / 220 / 217 ...);
  * both console scripts exist and point at esp32s3_hw_mcp.server:main;
  * every tool name the registry routes to is a def in the wheel's server module.

Reads only the wheel and the checkout: no network, no Espressif toolchain, no extraction run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from esp32s3_hw_mcp import __version__                                # noqa: E402
from esp32s3_hw_mcp import registry                                    # noqa: E402

FAILED: list[str] = []
CHECKED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKED
    CHECKED += 1
    if ok:
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} -- {detail}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wheel", help="path to the built .whl")
    a = ap.parse_args()
    with zipfile.ZipFile(a.wheel) as zf:
        names = set(zf.namelist())
        print(f"wheel: {os.path.basename(a.wheel)}  ({len(names)} entries)")

        # 1. every shipped artifact, byte for byte against the checkout
        shipped = [x for x in registry.ARTIFACTS
                   if x["in_wheel"] and x["route_kind"] in ("extracted", "measured")]
        for art in shipped:
            rel = f"esp32s3_hw_mcp/data/{art['file']}"
            if rel not in names:
                check(f"{art['id']}: {rel} is inside the wheel", False, "missing from the wheel")
                continue
            repo_path = os.path.join(ROOT, "data", art["file"])
            same = (sha256_bytes(zf.read(rel)) == sha256_file(repo_path)) if os.path.exists(repo_path) \
                else None
            check(f"{art['id']}: {art['file']} ships and matches the checkout",
                  same is True, "stale wheel (built from another commit?)" if same is False
                  else "not in the checkout to compare")
            if art["count"]:
                records = art["count"](repo_path) if os.path.exists(repo_path) else None
                inside = json.loads(zf.read(rel).decode("utf-8"))
                key = None
                # count the wheel's own copy the same way the registry counts the checkout's
                if art["id"] in ("registers", "pie_instructions", "pie_pipeline", "pie_review",
                                 "peripheral_map"):
                    inside_n = len(inside)
                else:
                    key = {"pie_timing_measured": "measurements", "pie_examples_measured": "findings",
                           "pie_encoding_errata": "instructions"}.get(art["id"])
                    inside_n = len(inside[key]) if key else sum(
                        len(inside[s["name"]]) for s in inside["section_index"])
                check(f"{art['id']}: the wheel's own copy counts {inside_n} {art['unit']}",
                      records is None or inside_n == records, f"checkout says {records}")

        # 2. the pieces the server runs itself
        for rel, why in [("esp32s3_hw_mcp/server.py", "the server"),
                         ("esp32s3_hw_mcp/asm_toolchain.py", "the assembler bridge (check_asm et al.)"),
                         ("esp32s3_hw_mcp/build_corpus.py", "the corpus builder (--fetch-corpus)"),
                         ("esp32s3_hw_mcp/registry.py", "the route index"),
                         ("esp32s3_hw_mcp/NOTICE.md", "the attribution notice")]:
            check(f"{rel} ships ({why})", rel in names, "missing")

        # 3. the console scripts
        eps = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
        check("the wheel declares entry points", bool(eps), str([n for n in names if "dist-info" in n])[:200])
        if eps:
            text = zf.read(eps[0]).decode("utf-8")
            for script in ("esp32s3-hw-mcp", "esp32-hw-mcp"):
                check(f"console script {script} -> esp32s3_hw_mcp.server:main",
                      re.search(rf"^{re.escape(script)}\s*=\s*esp32s3_hw_mcp\.server:main\s*$", text,
                                re.M) is not None, text[:200])
        metas = [n for n in names if n.endswith(".dist-info/METADATA")]
        if metas:
            meta = zf.read(metas[0]).decode("utf-8")
            check("the wheel requires the MCP SDK", "Requires-Dist: mcp" in meta,
                  "\n".join(l for l in meta.splitlines() if l.startswith("Requires"))[:200])
            check(f"the wheel states the version the package reports ({__version__})",
                  f"Version: {__version__}" in meta,
                  "\n".join(l for l in meta.splitlines() if l.startswith("Version"))[:80])
            check("the wheel keeps the licence stance (no licence granted) and ships the notice",
                  "License-File: NOTICE.md" in meta or "License: LicenseRef-Proprietary" in meta,
                  "\n".join(l for l in meta.splitlines() if l.startswith("License"))[:120])

        # 4. the surface the registry promises is in the shipped module
        server_src = zf.read("esp32s3_hw_mcp/server.py").decode("utf-8") if "esp32s3_hw_mcp/server.py" \
            in names else ""
        routed = sorted({t for x in registry.ARTIFACTS for t in x["presenters"]["tools"]})
        missing = [t for t in routed if not re.search(rf"^\s*def {re.escape(t)}\(", server_src, re.M)]
        check(f"all {len(routed)} tools the registry routes to are defined in the shipped server",
              not missing, str(missing))

    print(f"\n{'FAILED' if FAILED else 'PASSED'}: {CHECKED} checks"
          + (f", {len(FAILED)} failure(s)" if FAILED else ""))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
