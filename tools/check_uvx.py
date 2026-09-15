#!/usr/bin/env python3
"""Check the published route: `uvx --from <spec> esp32s3-hw-mcp` starts and lists its tools.

    .venv/bin/python tools/check_uvx.py --spec .
    .venv/bin/python tools/check_uvx.py --spec "git+file://$PWD"
    .venv/bin/python tools/check_uvx.py --spec git+https://github.com/dj-oyu/esp32s3-hw-mcp

This is the check for the path users actually take. Everything else in CI builds the wheel in place; here uv
resolves the spec, builds/installs into its own cache environment, and runs the console script -- so it
catches what only that path can break: a missing entry point, a data file left out of the wheel, an install
whose knowledge resolves to nothing, a server that dies before `tools/list`.

Each spec is checked with `--version`, `--list` (the tool surface a human sees), `--registry` (the route
index: which copy of the knowledge answered, and whether anything required is missing), and the short alias.
The expected surface is read from esp32s3_hw_mcp/registry.py, so this cannot pass while the index and the
tools disagree.

uvx must be on PATH; if it is not, this reports that and exits 2 (a CI job must install uv first).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from esp32s3_hw_mcp import __version__ as VERSION                     # noqa: E402
from esp32s3_hw_mcp import registry                                    # noqa: E402

FAILED: list[str] = []
CHECKED = 0
TOOL_LINE = re.compile(r"^-\s+([A-Za-z_][A-Za-z0-9_]*):")


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKED
    CHECKED += 1
    if ok:
        print(f"    ok    {label}")
    else:
        FAILED.append(label)
        print(f"    FAIL  {label} -- {detail}")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=900)


def check_spec(spec: str, refresh: bool) -> None:
    """One spec: resolve it with uvx, then read the server's own answers back."""
    if spec in (".", "..") or (not spec.startswith(("git+", "http", "file:")) and os.path.isdir(spec)):
        spec = os.path.abspath(spec)           # a path spec must not depend on the caller's cwd
    print(f"\n  spec: {spec}")
    base = ["uvx"] + (["--refresh"] if refresh else []) + ["--from", spec]

    r = run(base + ["esp32s3-hw-mcp", "--version"])
    check("uvx --from <spec> esp32s3-hw-mcp --version runs and reports the packaged version",
          r.returncode == 0 and r.stdout.strip() == f"esp32s3-hw-mcp {VERSION}",
          f"exit={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()[-300:]!r}")

    r = run(base + ["esp32-hw-mcp", "--version"])
    check("the short alias esp32-hw-mcp resolves to the same server",
          r.returncode == 0 and r.stdout.strip() == f"esp32s3-hw-mcp {VERSION}",
          f"exit={r.returncode} stdout={r.stdout.strip()!r}")

    r = run(base + ["esp32s3-hw-mcp", "--list"])
    listed = [m.group(1) for m in (TOOL_LINE.match(line) for line in r.stdout.splitlines()) if m]
    expected = sorted({t for a in registry.ARTIFACTS for t in a["presenters"]["tools"]})
    check(f"uvx --from <spec> esp32s3-hw-mcp --list prints all {len(expected)} tools the registry routes to",
          r.returncode == 0 and sorted(listed) == expected,
          f"exit={r.returncode} missing={sorted(set(expected) - set(listed))} "
          f"extra={sorted(set(listed) - set(expected))} stderr={r.stderr.strip()[-200:]!r}")
    check("--list reports a tool count that matches what it printed",
          f"{len(listed)} tools;" in r.stdout, r.stdout.strip()[:200])

    r = run(base + ["esp32s3-hw-mcp", "--registry"])
    try:
        reg = json.loads(r.stdout)
    except Exception as exc:
        reg = None
        check("uvx --from <spec> esp32s3-hw-mcp --registry returns the route index as JSON", False,
              f"{type(exc).__name__}: {exc}; stderr={r.stderr.strip()[-200:]!r}")
    if reg is not None:
        summary = reg["summary"]
        check("--registry returns the route index as JSON", r.returncode == 0, f"exit={r.returncode}")
        check("nothing required is missing in the installed copy",
              summary["missing_required"] == [], json.dumps(summary["missing_required"]))
        check("the knowledge answered from the copy inside the installed package (uvx), not a checkout",
              reg["roots"]["data"]["source"] == "wheel",
              json.dumps(reg["roots"]["data"])[:200])
        shipped = [a for a in reg["artifacts"]
                   if a["shipped_in_wheel"] and a["route_kind"] in ("extracted", "measured")]
        check(f"all {len(shipped)} shipped artifacts are present",
              all(a["present"] for a in shipped),
              json.dumps([a["id"] for a in shipped if not a["present"]]))
        check("the index lists the same surface --list printed (index and server agree)",
              sorted(set(summary["tools_reached"])) == sorted(listed),
              f"index={sorted(summary['tools_reached'])} listed={sorted(listed)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", action="append", default=[],
                    help="a uvx --from spec (path, git+file://..., git+https://...); repeatable")
    ap.add_argument("--refresh", action="store_true",
                    help="pass --refresh to uvx (re-resolve the pin instead of using its cache)")
    a = ap.parse_args()
    specs = a.spec or ["."]
    if not shutil.which("uvx"):
        print("uvx is not on PATH -- install uv first (https://docs.astral.sh/uv/)", file=sys.stderr)
        return 2
    print(f"uvx: {shutil.which('uvx')}")
    print(f"expected surface: {len({t for x in registry.ARTIFACTS for t in x['presenters']['tools']})} "
          f"tools, version {VERSION}")
    for spec in specs:
        check_spec(spec, a.refresh or spec.startswith("git+https"))
    print(f"\n{'FAILED' if FAILED else 'PASSED'}: {CHECKED} checks"
          + (f", {len(FAILED)} failure(s)" if FAILED else ""))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
