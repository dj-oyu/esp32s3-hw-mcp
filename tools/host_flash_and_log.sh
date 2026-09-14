#!/usr/bin/env bash
# Flash the PIE timing firmware onto the Cardputer and capture its report -- meant to run on the WSL host.
#
#   bash /workspace/esp32s3-hw-mcp/tools/host_flash_and_log.sh                # flash, read back, capture, parse
#   bash ... --no-flash                                                      # capture from whatever is flashed
#   bash ... --backup                                                        # dump the whole 8 MB first
#   bash ... --restore /workspace/backups/cardputer-s3-<stamp>.bin           # put a dump back
#   bash ... --dry-run                                                       # say what it would do, touch nothing
#   PORT=/dev/ttyACM1 bash ...                                               # if the port number moved
#
# Why on the host: the container's /dev entry is bound at start-up to whatever inode existed then, so a USB
# re-enumeration (which every esptool run triggers) can leave it pointing at a deleted inode -- mode 0000,
# open() refused, unrecoverable from inside (no CAP_MKNOD / CAP_SYS_ADMIN). The host's own /dev/ttyACM0 is
# created fresh by the kernel and is always the live one, so the host is where the flashing runs reliably.
#
# Needs only esptool + pyserial: the firmware is already built in this repository (pietiming bins), so no
# ESP-IDF is required on the host. Outputs land next to this repository, i.e. in the same /workspace tree
# the container sees, so the logs can be parsed from either side.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO/experiments/pie-timing/firmware/build_pietiming"
OUT_DIR="${OUT_DIR:-$(dirname "$REPO")/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

DO_FLASH=1
DO_BACKUP=0
DO_PARSE=1
DRY_RUN=0
RESTORE=""
PORT="${PORT:-}"
TIMEOUT="${TIMEOUT:-240}"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-flash) DO_FLASH=0 ;;
    --backup)   DO_BACKUP=1 ;;
    --no-parse) DO_PARSE=0 ;;
    --dry-run)  DRY_RUN=1 ;;
    --port)     PORT="${2:?--port needs a device path}"; shift ;;
    --restore)  RESTORE="${2:?--restore needs a dump path}"; shift ;;
    --out-dir)  OUT_DIR="${2:?--out-dir needs a path}"; shift ;;
    -h|--help)  sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s ==\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

# ---------------------------------------------------------------- interpreter with esptool + pyserial
has_modules() {
  "$1" - <<'PY' >/dev/null 2>&1
import importlib.util as u, sys
need = ["esptool", "serial"]
missing = [n for n in need if u.find_spec(n) is None]
sys.exit(1 if missing else 0)
PY
}

PY=""
for cand in ${ESP_PYTHON:-} "$REPO/.venv-host/bin/python" "$HOME/.venv-esp/bin/python" \
            "$HOME/.espressif/python_env/idf6.0_py3.11_env/bin/python" python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && has_modules "$(command -v "$cand")"; then
    PY="$(command -v "$cand")"
    break
  fi
done

if [ -z "$PY" ]; then
  cat >&2 <<'MSG'
error: no python3 with esptool AND pyserial was found on this host.

Install them into a private venv (no sudo, no system policy problems), then re-run:

  python3 -m venv ~/.venv-esp
  ~/.venv-esp/bin/pip install --upgrade esptool pyserial
  bash /workspace/esp32s3-hw-mcp/tools/host_flash_and_log.sh

(or point ESP_PYTHON at an interpreter that already has both)
MSG
  exit 2
fi

say "repository : $REPO"
say "build dir  : $BUILD"
say "output dir : $OUT_DIR"
say "python     : $PY"
say "port       : ${PORT:-<auto>}"

# ---------------------------------------------------------------- port
if [ -z "$PORT" ]; then
  # shellcheck disable=SC2012
  PORT="$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1 || true)"
fi
[ -n "$PORT" ] || die "no /dev/ttyACM* or /dev/ttyUSB* on this host. Attach the device first (usbipd), and
       check it arrived: ls -l /dev/ttyACM*"

if [ ! -e "$PORT" ]; then
  die "$PORT does not exist on this host. ls -l /dev/ttyACM* to see what did arrive."
fi
say "using      : $PORT"

if [ "$DRY_RUN" = 0 ]; then
  if ! "$PY" - "$PORT" <<'PY'
import os, stat, sys
p = sys.argv[1]
st = os.stat(p)
if not stat.S_ISCHR(st.st_mode):
    print(f"{p} is not a character device", file=sys.stderr); sys.exit(1)
if os.major(st.st_rdev) == 166:
    pass                      # USB CDC-ACM: what the ESP32-S3 USB-Serial-JTAG shows up as
try:
    os.close(os.open(p, os.O_RDWR | os.O_NONBLOCK))
except OSError as exc:
    print(f"cannot open {p}: {exc}", file=sys.stderr)
    print("if mode is c--------- and the device is attached, this is the stranded-container-node case "
          "described in notes/05-resume.md", file=sys.stderr)
    sys.exit(1)
PY
  then
    die "$PORT exists but cannot be opened (see the message above)"
  fi
  say "port check : openable"
fi

mkdir -p "$OUT_DIR"
if [ "$DRY_RUN" = 0 ] && ! touch "$OUT_DIR/.write-test" 2>/dev/null; then
  die "$OUT_DIR is not writable by $(id -un). Pass --out-dir with a directory you own."
fi
rm -f "$OUT_DIR/.write-test"

# ---------------------------------------------------------------- restore mode
if [ -n "$RESTORE" ]; then
  step "restore: $RESTORE (whole 8 MB at 0x0)"
  [ -f "$RESTORE" ] || die "no such dump: $RESTORE"
  if [ -f "$RESTORE.sha256" ] && [ "$DRY_RUN" = 0 ]; then
    (cd "$(dirname "$RESTORE")" && sha256sum -c "$(basename "$RESTORE").sha256")
  fi
  run "$PY" "$REPO/tools/check_flash_dump.py" "$RESTORE"
  run "$PY" -m esptool --chip esp32s3 --port "$PORT" --baud 921600 write_flash 0x0 "$RESTORE"
  say "restored. The chip now holds whatever that dump contained."
  exit 0
fi

# ---------------------------------------------------------------- flash offsets from the build itself
FLASH_PAIRS=()
if [ "$DO_FLASH" = 1 ]; then
  [ -f "$BUILD/flasher_args.json" ] || die "$BUILD/flasher_args.json is missing -- build it first:
       cd $REPO && bash tools/build_pie_timing.sh"
  while read -r off path; do
    FLASH_PAIRS+=("$off" "$path")
  done < <("$PY" - "$BUILD/flasher_args.json" <<'PY'
import json, os, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
base = os.path.dirname(os.path.abspath(sys.argv[1]))
for off, rel in sorted(d["flash_files"].items(), key=lambda kv: int(kv[0], 16)):
    print(off, os.path.join(base, rel))
PY
)
  [ "${#FLASH_PAIRS[@]}" -ge 4 ] || die "could not read the flash offsets from flasher_args.json"
  # The application image is the one that is neither the bootloader nor the partition table; its offset and
  # size are what the read-back step needs.
  APP_OFF=""; APP_BIN=""
  for ((i = 0; i < ${#FLASH_PAIRS[@]}; i += 2)); do
    case "${FLASH_PAIRS[i + 1]}" in
      */bootloader/*|*/partition_table/*) continue ;;
      *) APP_OFF="${FLASH_PAIRS[i]}"; APP_BIN="${FLASH_PAIRS[i + 1]}" ;;
    esac
  done
  [ -n "$APP_BIN" ] || die "no application image found in flasher_args.json"
fi

# ---------------------------------------------------------------- 1. identify
step "1/5 identify"
run "$PY" -m esptool --chip esp32s3 --port "$PORT" --before default-reset --after no-reset flash_id
if [ "$DRY_RUN" = 0 ]; then
  "$PY" -m esptool --chip esp32s3 --port "$PORT" --before no-reset --after no-reset flash_id \
    > "$OUT_DIR/flash_id-$STAMP.txt" 2>&1 || true
fi

# ---------------------------------------------------------------- 2. optional backup
if [ "$DO_BACKUP" = 1 ]; then
  step "2/5 backup the whole 8 MB (this is the only copy of what is on the chip now)"
  DUMP="$OUT_DIR/cardputer-s3-$STAMP.bin"
  run "$PY" -m esptool --chip esp32s3 --port "$PORT" --baud 921600 --before default-reset --after no-reset \
      read_flash 0 0x800000 "$DUMP"
  if [ "$DRY_RUN" = 0 ]; then
    (cd "$OUT_DIR" && sha256sum "$(basename "$DUMP")" | tee "$(basename "$DUMP").sha256")
    "$PY" "$REPO/tools/check_flash_dump.py" "$DUMP"
  fi
fi

# ---------------------------------------------------------------- 3. flash
if [ "$DO_FLASH" = 1 ]; then
  step "3/5 flash the measurement firmware (--after no-reset: the chip stays in the loader)"
  run "$PY" -m esptool --chip esp32s3 --port "$PORT" --baud 921600 --before default-reset --after no-reset \
      write_flash --flash-mode dio --flash-freq 80m --flash-size 8MB "${FLASH_PAIRS[@]}"

  step "4/5 read the app region back and compare hashes (a write nobody checked is not a write)"
  APP_SIZE="$(stat -c %s "$APP_BIN")"
  READBACK="$OUT_DIR/host-readback-$STAMP.bin"
  if [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] read_flash $APP_OFF $APP_SIZE -> $READBACK, then compare sha256 with $APP_BIN"
  else
    "$PY" -m esptool --chip esp32s3 --port "$PORT" --baud 921600 --before no-reset --after no-reset \
      read_flash "$APP_OFF" "$APP_SIZE" "$READBACK" >/dev/null
    if [ "$(sha256sum < "$APP_BIN")" != "$(sha256sum < "$READBACK")" ]; then
      die "the bytes at $APP_OFF do not match the built image:
       built    : $(sha256sum < "$APP_BIN")
       read back: $(sha256sum < "$READBACK")"
    fi
    say "flash verified: $APP_SIZE bytes at $APP_OFF match $(basename "$APP_BIN")"
    rm -f "$READBACK"
  fi
else
  step "3/5-4/5 skipped (--no-flash): capturing from whatever is already flashed"
fi

# ---------------------------------------------------------------- 5. capture
step "5/5 reset into the application and capture the serial report"
LOG="$OUT_DIR/pie-timing-$STAMP.log"
reset_into_app() {
  # One explicit reset leaves the loader and starts the application. On the host this costs a USB
  # re-enumeration, which is harmless here (the kernel re-creates /dev/ttyACM0) -- it is the container that
  # cannot follow it, which is exactly why this script runs on the host.
  run "$PY" -m esptool --chip esp32s3 --port "$PORT" --before no-reset --after hard-reset read_mac \
      >/dev/null 2>&1 || true
}
capture() {
  run "$PY" "$REPO/tools/capture_serial.py" --port "$PORT" --out "$LOG" --timeout "$TIMEOUT"
}
if [ "$DRY_RUN" = 0 ]; then
  cat >&2 <<'MSG'
note: the firmware waits for one byte from the host and then prints its whole report, so capture_serial.py
      sends that byte after opening the port. If the chip was left in the loader it is reset into the
      application first; either way the report is printed after the byte arrives, not before.
MSG
fi
reset_into_app
if ! capture; then
  say "note: no END marker -- resetting once more and capturing again" >&2
  reset_into_app
  capture || say "note: still no END marker; the log is written and will be parsed anyway"
fi

if [ "$DRY_RUN" = 1 ]; then
  say "  [dry-run] would write $LOG"
  exit 0
fi

# ---------------------------------------------------------------- parse + summary
say ""
say "log        : $LOG"
say "container  : /workspace/${LOG#"$(dirname "$REPO")/"}"
say "             (the host's $(dirname "$REPO") is bind-mounted at /workspace inside the pod, so the"
say "              agent can read this file directly)"
if [ "$DO_PARSE" = 1 ]; then
  REV="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  say "firmware   : $REV"
  say ""
  "$PY" "$REPO/tools/parse_pie_timing.py" "$LOG" --firmware-rev "$REV" \
      --out "$REPO/data/pie_timing_measured.json" || true
fi
say ""
say "first lines of the report:"
grep -m6 -E "^(ENV|ROUND|HOST|BEGIN)" "$LOG" 2>/dev/null | sed 's/^/  /' || true
say ""
say "next: tell the agent the log path and it will pick the result up from /workspace/backups."
