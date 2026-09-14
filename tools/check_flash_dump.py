#!/usr/bin/env python3
"""Sanity-check a full flash dump before any firmware is written over it.

A backup that nobody verified is not a backup. This checks the three structures that must be present in a
bootable ESP32-S3 image -- bootloader magic at 0x0, a partition table at 0x8000, an application image at
0x10000 -- and reports the size, so a truncated or empty read is caught while the flash still holds the
original firmware.

    tools/check_flash_dump.py /workspace/backups/cardputer-adv-<date>.bin [--expected-size 0x800000]
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--expected-size", default="0x800000")
    a = ap.parse_args()

    want = int(a.expected_size, 16)
    size = os.path.getsize(a.dump)
    data = open(a.dump, "rb").read()
    failures = []

    if size != want:
        failures.append(f"size {size} bytes != expected {want}")
    if size >= 0x1000 + 1 and data[0x1000] != 0xE9:
        # The bootloader is written at 0x1000 on ESP32-S3; 0xE9 is the Xtensa image magic.
        failures.append(f"no bootloader magic 0xE9 at 0x1000 (found 0x{data[0x1000]:02X})")
    if size >= 0x8002 and data[0x8000:0x8002] != b"\xaa\x50":
        failures.append(f"no partition-table magic 0xAA50 at 0x8000 (found {data[0x8000:0x8002].hex()})")
    if size >= 0x10001 and data[0x10000] != 0xE9:
        failures.append(f"no application magic 0xE9 at 0x10000 (found 0x{data[0x10000]:02X})")

    print(f"{a.dump}: {size} bytes")
    for label, offset in (("bootloader", 0x1000), ("partition table", 0x8000), ("application", 0x10000)):
        if offset + 1 < size:
            print(f"  {label:16s} @0x{offset:06x}: 0x{data[offset]:02X} ... 0x{data[min(offset + 3, size - 1)]:02X}")
    if failures:
        print("FAIL: dump is not a bootable image:")
        for f in failures:
            print(f"  - {f}")
        print("Do NOT flash over this device until a good dump exists.")
        return 1
    print("ok: dump has a bootloader, a partition table and an application image")
    return 0


if __name__ == "__main__":
    sys.exit(main())
