#!/usr/bin/env bash
# Run the PIE timing experiment on a real ESP32-S3.
#
#   PORT=/dev/ttyACM0 BACKUP_DIR=/workspace/backups bash tools/device_experiment.sh
#
# Order is deliberate and not negotiable:
#   1. identify the chip (esptool flash_id) and dump the ENTIRE flash, with a sha256 and a structural check
#   2. refuse to continue unless that dump looks like a bootable image
#   3. build, flash the measurement firmware, capture the serial log
#   4. parse the log into data/pie_timing_measured.json (and fail loudly if the anchors do not reproduce)
#
# The dump is the only copy of whatever was on the device: tools/restore_flash.sh puts it back.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PORT="${PORT:-/dev/ttyACM0}"
BAUD="${BAUD:-921600}"
BACKUP_DIR="${BACKUP_DIR:-/workspace/backups}"
FLASH_SIZE="${FLASH_SIZE:-0x800000}"          # ESP32-S3FN8 = 8 MB
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="$BACKUP_DIR/cardputer-s3-$STAMP.bin"
LOG="$BACKUP_DIR/pie-timing-$STAMP.log"

. /opt/esp-idf/export.sh >/dev/null 2>&1
mkdir -p "$BACKUP_DIR"

if [ ! -e "$PORT" ]; then
  echo "port $PORT does not exist. Inside a container this usually means the device was not passed through:" >&2
  echo "  start the container with:  --device=$PORT" >&2
  exit 2
fi

echo "== 1/5 identify =="
esptool --chip esp32s3 --port "$PORT" flash_id | tee "$BACKUP_DIR/flash_id-$STAMP.txt"

echo "== 2/5 backup ($FLASH_SIZE bytes -> $DUMP) =="
esptool --chip esp32s3 --port "$PORT" --baud "$BAUD" read_flash 0 "$FLASH_SIZE" "$DUMP"
sha256sum "$DUMP" | tee "$DUMP.sha256"
python3 tools/check_flash_dump.py "$DUMP" --expected-size "$FLASH_SIZE"

echo "== 3/5 build =="
bash tools/build_pie_timing.sh

echo "== 4/5 flash + capture =="
FW_REV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
. /opt/esp-idf/export.sh >/dev/null 2>&1
# idf.py resolves the project from the cwd, and the build dir has to be the one build_pie_timing.sh
# created -- so flash from inside the firmware project, in a subshell that leaves our cwd alone.
( cd experiments/pie-timing/firmware && idf.py -B build_pietiming -p "$PORT" flash )

# A write that was not read back is not a write: the app region is read off the chip again and hashed
# against the image that was just built, so a silently failed or short write stops the run here.
APP_BIN="experiments/pie-timing/firmware/build_pietiming/pie_timing.bin"
APP_OFF="$(python3 -c "import json; d=json.load(open('experiments/pie-timing/firmware/build_pietiming/flasher_args.json'))['flash_files']; print([k for k, v in d.items() if v.endswith('pie_timing.bin')][0])")"
APP_SIZE="$(stat -c %s "$APP_BIN")"
READBACK="$BACKUP_DIR/readback-$STAMP.bin"
esptool --chip esp32s3 --port "$PORT" --baud "$BAUD" read_flash "$APP_OFF" "$APP_SIZE" "$READBACK" >/dev/null
if [ "$(sha256sum < "$APP_BIN")" != "$(sha256sum < "$READBACK")" ]; then
  echo "FAIL: $APP_OFF in flash does not match the built image -- the write did not land" >&2
  echo "  built    : $(sha256sum < "$APP_BIN")" >&2
  echo "  read back: $(sha256sum < "$READBACK")" >&2
  exit 1
fi
echo "flash verified: $APP_SIZE bytes at $APP_OFF match $APP_BIN"

if ! /root/.espressif/python_env/idf6.0_py3.11_env/bin/python tools/capture_serial.py \
     --port "$PORT" --out "$LOG" --timeout 240; then
  # A capture that never saw END is still worth parsing: what did arrive says why.
  echo "note: capture finished without the END marker -- parsing the log anyway" >&2
fi

echo "== 5/5 parse =="
python3 tools/parse_pie_timing.py "$LOG" --firmware-rev "$FW_REV"

echo
echo "backup : $DUMP  (restore with: PORT=$PORT bash tools/restore_flash.sh $DUMP)"
echo "log    : $LOG"
echo "result : data/pie_timing_measured.json"
