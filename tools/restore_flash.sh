#!/usr/bin/env bash
# Put a flash dump back on the device -- the undo for tools/device_experiment.sh.
#
#   PORT=/dev/ttyACM0 bash tools/restore_flash.sh /workspace/backups/cardputer-s3-<stamp>.bin
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-/dev/ttyACM0}"
DUMP="${1:?usage: restore_flash.sh <dump.bin>}"
. /opt/esp-idf/export.sh >/dev/null 2>&1

python3 tools/check_flash_dump.py "$DUMP"
if [ -f "$DUMP.sha256" ]; then
  sha256sum -c "$DUMP.sha256"
fi
echo "writing $DUMP back to $PORT (whole chip, all 8 MB)"
esptool --chip esp32s3 --port "$PORT" --baud 921600 write_flash 0x0 "$DUMP"
echo "done -- the device is back to its dumped state"
