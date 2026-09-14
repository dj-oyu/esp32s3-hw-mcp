#!/usr/bin/env bash
# Scratch: build + flash + capture the current firmware, without re-dumping the flash.
# Used while diagnosing the PIE fault; tools/device_experiment.sh stays the real entry point.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-/dev/ttyACM0}"
OUT="${OUT:-/workspace/backups/probe.log}"
TIMEOUT="${TIMEOUT:-30}"
. /opt/esp-idf/export.sh >/dev/null 2>&1

( cd experiments/pie-timing/firmware
  if ! idf.py -B build_pietiming build >/tmp/probe_build.log 2>&1; then
    echo "BUILD FAILED -- tail of /tmp/probe_build.log:" >&2
    tail -30 /tmp/probe_build.log >&2
    exit 1
  fi
  idf.py -B build_pietiming -p "$PORT" flash >/tmp/probe_flash.log 2>&1 )
echo "built + flashed"
/root/.espressif/python_env/idf6.0_py3.11_env/bin/python tools/capture_serial.py \
  --port "$PORT" --out "$OUT" --timeout "$TIMEOUT" --end-marker "PROBE end" || true
echo "log: $OUT"
