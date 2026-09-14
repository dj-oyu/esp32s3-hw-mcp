#!/usr/bin/env bash
# Build the PIE examples firmware (ESP-IDF v6.0.1 in this image: /opt/esp-idf).
#
#   bash tools/build_examples.sh            # build examples/firmware -> build_examples/pie_examples.bin
#   bash tools/build_examples.sh --clean    # start from an empty build directory
set -euo pipefail
cd "$(dirname "$0")/.."
BUILD="${BUILD:-build_examples}"
if [ "${1:-}" = "--clean" ]; then
  rm -rf "examples/firmware/$BUILD"
fi
. /opt/esp-idf/export.sh >/dev/null 2>&1
cd examples/firmware
idf.py -B "$BUILD" set-target esp32s3 >/dev/null
idf.py -B "$BUILD" build "$@"
echo "-> $(pwd)/$BUILD/pie_examples.bin"
