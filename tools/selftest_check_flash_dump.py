#!/usr/bin/env python3
"""Self-test for tools/check_flash_dump.py -- build synthetic ESP32-S3 flash images and try to sneak
each kind of broken dump past the gate.

The gate exists to stop a firmware write when the only copy of what was on the device is not a good
copy. It was previously written with the ESP32-classic bootloader offset (0x1000) and would have
refused every real ESP32-S3 dump -- and blocked restores too. A gate that is never exercised against
a dump it must reject, and one it must accept, is not a gate.

    python3 tools/selftest_check_flash_dump.py
"""
from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "check_flash_dump.py")

FLASH_SIZE = 0x800000
CHIP_ID = 9
BOOT_OFF = 0x0
PT_OFF = 0x8000
APP_OFF = 0x10000


def make_image(entry: int, segments: list[tuple[int, bytes]], digest: bool = True,
               chip_id: int = CHIP_ID) -> bytes:
    """One ESP32-S3 image exactly as esptool lays it out: 24-byte header, segments, padded body,
    then the SHA-256 of everything before it."""
    hdr = bytearray(24)
    hdr[0] = 0xE9
    hdr[1] = len(segments)
    hdr[2] = 0x02
    hdr[3] = 0x3F
    struct.pack_into("<I", hdr, 4, entry)
    hdr[8] = 0xEE                      # wp_pin: not used
    struct.pack_into("<H", hdr, 12, chip_id)
    hdr[17] = 0x63                     # max_chip_rev_full = 99 (any revision)
    hdr[23] = 1 if digest else 0
    body = bytes(hdr)
    for addr, data in segments:
        body += struct.pack("<II", addr, len(data)) + data
    body += b"\x00" * (-len(body) % 16)
    if digest:
        body += hashlib.sha256(body).digest()
    return body


def make_dump() -> bytes:
    """A bootable-looking 8 MB ESP32-S3 flash: bootloader @0x0, partition table @0x8000, app @0x10000."""
    flash = bytearray(b"\xFF" * FLASH_SIZE)
    boot = make_image(0x403C8904, [(0x3FCE2820, b"\x01" * 512), (0x403C8700, b"\x02" * 256)])
    flash[BOOT_OFF:BOOT_OFF + len(boot)] = boot

    table = b""
    for typ, sub, off, size, label in (
        (1, 0x02, 0x9000, 0x6000, b"nvs"),
        (1, 0x01, 0xF000, 0x1000, b"phy_init"),
        (0, 0x00, APP_OFF, 0x300000, b"factory"),
    ):
        table += struct.pack("<HBBII", 0x50AA, typ, sub, off, size) + label.ljust(16, b"\x00") + struct.pack("<I", 0)
    table += struct.pack("<HBBII", 0xEBEB, 0, 0, 0, 0) + b"\xFF" * 16 + struct.pack("<I", 0)
    flash[PT_OFF:PT_OFF + len(table)] = table

    app = make_image(0x40375818, [(0x3C190020, b"\xAA" * 4096), (0x42000020, b"\xBB" * 8192)])
    flash[APP_OFF:APP_OFF + len(app)] = app
    return bytes(flash)


def run(dump_path: str, *extra: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, CHECK, dump_path, *extra],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    good = make_dump()
    cases: list[tuple[str, bytes, list[str], int]] = []

    cases.append(("intact 8 MB dump", good, [], 0))
    cases.append(("truncated read (4 MB)", good[:0x400000], [], 1))
    cases.append(("empty read (all 0xFF)", b"\xFF" * FLASH_SIZE, [], 1))

    b = bytearray(good); b[BOOT_OFF + 0] = 0x2F                     # bootloader magic gone
    cases.append(("bootloader magic wrong", bytes(b), [], 1))

    b = bytearray(good); b[BOOT_OFF + 12] = 2                       # bootloader built for esp32s2
    cases.append(("bootloader chip id wrong", bytes(b), [], 1))

    b = bytearray(good); b[PT_OFF:PT_OFF + 2] = b"\x00\x00"         # partition table magic gone
    cases.append(("partition table magic wrong", bytes(b), [], 1))

    b = bytearray(good); b[APP_OFF + 0] = 0x00                      # app image magic gone
    cases.append(("app magic wrong", bytes(b), [], 1))

    b = bytearray(good); b[APP_OFF + 0x18 + 8 + 100] ^= 0xFF        # one byte inside the app body
    cases.append(("app body corrupted (digest must catch)", bytes(b), [], 1))
    cases.append(("app body corrupted, digest check waived", bytes(b), ["--allow-missing-digest"], 0))

    b = bytearray(good)                                             # app moved 4 KB off its partition
    b[APP_OFF:APP_OFF + 0x2000] = good[APP_OFF + 0x1000:APP_OFF + 0x3000]
    cases.append(("app image not at its partition offset", bytes(b), [], 1))

    # A dump that holds an image for a different chip in the app partition, everything else fine.
    flash = bytearray(good)
    app = make_image(0x40375818, [(0x3C190020, b"\xCC" * 2048)], chip_id=5)
    flash[APP_OFF:APP_OFF + 0x2000] = app + b"\xFF" * (0x2000 - len(app))
    cases.append(("app built for another chip (esp32c3)", bytes(flash), [], 1))

    failures = 0
    with tempfile.TemporaryDirectory() as td:
        for i, (name, data, extra, want_rc) in enumerate(cases):
            path = os.path.join(td, f"case{i}.bin")
            with open(path, "wb") as fh:
                fh.write(data)
            rc, out = run(path, *extra)
            ok = rc == want_rc
            failures += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'FAIL'}  rc={rc} (want {want_rc})  {name}"
                  + (f"   [{out.strip().splitlines()[-3] if out.strip() else ''}]" if not ok else ""))
            if not ok:
                print("        " + "\n        ".join(out.strip().splitlines()[-5:]))

    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(cases) - failures}/{len(cases)} cases")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
