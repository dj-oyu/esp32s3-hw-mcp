#!/usr/bin/env python3
"""Capture the measurement firmware's serial output into a log file.

    tools/capture_serial.py --port /dev/ttyACM0 --out /tmp/pie_timing.log [--timeout 120]

Resets the chip (DTR/RTS, falling back to a plain reopen for USB-Serial-JTAG), reads until the firmware
prints its END marker or the timeout expires, and echoes what it read so a failure is visible immediately.

The port is opened right after esptool has reset the chip, and on ESP32-S3 USB-Serial-JTAG that window is
not always clean: the first read can raise "device reports readiness to read but returned no data". That
is a dropped port, not a missing measurement -- so each attempt reopens (and re-runs the firmware, which
prints everything at boot) until the END marker is seen or the attempts run out.
"""
from __future__ import annotations

import argparse
import sys
import time


def capture_once(port: str, baud: int, deadline: float, end_marker: str, settle: float) -> tuple[list[str], bool]:
    """One attempt. Returns (lines, saw_end). Raises on a dropped port."""
    import serial  # pyserial, from the ESP-IDF python env

    collected: list[str] = []
    if settle:
        time.sleep(settle)
    with serial.Serial(port, baud, timeout=1) as ser:
        # USB-Serial-JTAG on the S3 resets on a plain open; toggling DTR/RTS covers the UART-bridge case.
        try:
            ser.setDTR(False)
            ser.setRTS(True)
            time.sleep(0.1)
            ser.setRTS(False)
        except Exception:
            pass
        ser.reset_input_buffer()
        while time.time() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            collected.append(line)
            print(line, flush=True)
            if end_marker and line.strip() == end_marker:
                return collected, True
    return collected, False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--end-marker", default="END")
    ap.add_argument("--settle", type=float, default=1.5,
                    help="seconds to wait after the reset before opening the port")
    ap.add_argument("--attempts", type=int, default=4)
    a = ap.parse_args()

    try:
        import serial  # noqa: F401  (checked here so the error message names the right interpreter)
    except ImportError:
        print("pyserial missing: use the IDF python env "
              "(/root/.espressif/python_env/idf6.0_py3.11_env/bin/python)", file=sys.stderr)
        return 2

    collected: list[str] = []
    saw_end = False
    for attempt in range(1, a.attempts + 1):
        deadline = time.time() + a.timeout
        try:
            collected, saw_end = capture_once(a.port, a.baud, deadline, a.end_marker,
                                              a.settle if attempt == 1 else 0.5)
        except Exception as exc:                       # dropped/blocked port: reopen and start over
            print(f"capture attempt {attempt} lost the port ({exc}); reopening", file=sys.stderr)
            continue
        if saw_end:
            break
        print(f"capture attempt {attempt} saw no {a.end_marker!r}; retrying", file=sys.stderr)

    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(collected) + "\n")
    print(f"-> {a.out} ({len(collected)} lines, end marker {saw_end})")
    return 0 if saw_end else 1


if __name__ == "__main__":
    sys.exit(main())
