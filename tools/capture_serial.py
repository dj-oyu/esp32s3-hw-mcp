#!/usr/bin/env python3
"""Capture the measurement firmware's serial output into a log file.

    tools/capture_serial.py --port /dev/ttyACM0 --out /tmp/pie_timing.log [--timeout 120]

Resets the chip (DTR/RTS, falling back to a plain reopen for USB-Serial-JTAG), reads until the firmware
prints its END marker or the timeout expires, and echoes what it read so a failure is visible immediately.
"""
from __future__ import annotations

import argparse
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--end-marker", default="END")
    a = ap.parse_args()

    try:
        import serial  # pyserial, from the ESP-IDF python env
    except ImportError:
        print("pyserial missing: use the IDF python env "
              "(/root/.espressif/python_env/idf6.0_py3.11_env/bin/python)", file=sys.stderr)
        return 2

    collected: list[str] = []
    deadline = time.time() + a.timeout
    with serial.Serial(a.port, a.baud, timeout=1) as ser:
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
            if a.end_marker and line.strip() == a.end_marker:
                break

    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(collected) + "\n")
    print(f"-> {a.out} ({len(collected)} lines)")
    return 0 if any(l.strip() == a.end_marker for l in collected) else 1


if __name__ == "__main__":
    sys.exit(main())
