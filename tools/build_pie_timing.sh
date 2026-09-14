#!/usr/bin/env bash
# Build the PIE timing firmware (ESP-IDF v6.0.1 in this image: /opt/esp-idf).
set -euo pipefail
cd "$(dirname "$0")/.."
. /opt/esp-idf/export.sh >/dev/null 2>&1
cd experiments/pie-timing/firmware
python3 /workspace/esp32s3-hw-mcp/tools/gen_pie_timing_asm.py
idf.py -B build_pietiming set-target esp32s3 >/dev/null
idf.py -B build_pietiming build
echo "-> $(pwd)/build_pietiming/pie_timing.bin"
