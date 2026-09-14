#!/usr/bin/env python3
"""Sanity-check a full flash dump before any firmware is written over it.

A backup that nobody verified is not a backup. This checks the structures that must be present in a
bootable ESP32 image and, where the image carries one, re-verifies its own appended SHA-256, so a
truncated, shifted or half-read flash is caught while the flash still holds the original firmware.

    tools/check_flash_dump.py /workspace/backups/cardputer-s3-<date>.bin [--expected-size 0x800000]
    tools/check_flash_dump.py <dump> --chip esp32s3 --allow-missing-digest   # emergency restore

Chip facts (bootloader offset and image chip id) come from esptool's own target classes, which is what
actually flashes these parts:

    esptool/targets/esp32s3.py:  BOOTLOADER_FLASH_OFFSET = 0x0,  IMAGE_CHIP_ID = 9
    esptool/targets/esp32.py:    BOOTLOADER_FLASH_OFFSET = 0x1000, IMAGE_CHIP_ID = 0
    esptool/targets/esp32c3.py:  BOOTLOADER_FLASH_OFFSET = 0x0,  IMAGE_CHIP_ID = 5

(the 0x1000 in the old version of this file was the ESP32-classic offset, inherited by mistake: on the
ESP32-S3 the ROM loads the second-stage bootloader from 0x0, and 0x1000 is simply inside it.)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys

# chip -> (image chip id, second-stage bootloader flash offset). Mirrors esptool's targets/.
CHIPS: dict[str, tuple[int, int]] = {
    "esp32": (0, 0x1000),
    "esp32s2": (2, 0x1000),
    "esp32s3": (9, 0x0),
    "esp32c3": (5, 0x0),
    "esp32c6": (13, 0x0),
}

PART_MAGIC = 0x50AA
PART_MAGIC_END = 0xEBEB          # last entry of a partition table written by gen_esp32part.py
PART_TYPES = {0: "app", 1: "data"}
PART_SUBTYPES = {
    (0, 0x00): "factory", (0, 0x10): "ota_0", (0, 0x11): "ota_1", (0, 0x20): "test",
    (1, 0x00): "ota", (1, 0x01): "phy", (1, 0x02): "nvs", (1, 0x03): "coredump",
    (1, 0x04): "nvs_keys", (1, 0x05): "efuse", (1, 0x06): "undefined",
}
SEG_HEADER_LEN = 8
IMAGE_HEADER_LEN = 24            # 8-byte common header + 16-byte extended header


def align16(n: int) -> int:
    return ((n + 15) // 16) * 16


def parse_image(data: bytes, offset: int, chip_id: int) -> dict:
    """Decode one ESP image header + segment table at `offset`. Never raises on bad data."""
    out: dict = {"offset": offset, "ok": False, "problems": []}
    if offset + IMAGE_HEADER_LEN > len(data):
        out["problems"].append(f"image at 0x{offset:x} runs past the end of the dump")
        return out
    hdr = data[offset:offset + IMAGE_HEADER_LEN]
    magic, segment_count = hdr[0], hdr[1]
    # bytes 0x04..0x12: entry addr, wp_pin, 3 pin-drive bytes, chip id (uint16), min rev,
    # min rev full, max rev full -- the extended header as esptool's load_extended_header reads it.
    (
        out["entry"], out["wp_pin"], _, _, _, got_chip_id, out["min_rev"],
        out["min_chip_rev_full"], out["max_chip_rev_full"],
    ) = struct.unpack("<IBBBBHBHH", hdr[4:19])
    out["magic"] = magic
    out["segment_count"] = segment_count
    out["chip_id"] = got_chip_id
    out["append_digest"] = hdr[23] == 1        # 0x17, last byte of the 24-byte image header
    out["digest_flag_raw"] = hdr[23]

    if magic != 0xE9:
        out["problems"].append(f"no image magic 0xE9 at 0x{offset:x} (found 0x{magic:02X})")
    if got_chip_id != chip_id:
        out["problems"].append(f"image chip id {got_chip_id} at 0x{offset:x} is not {chip_id} "
                               f"(this image is for another chip)")
    if hdr[23] not in (0, 1):
        out["problems"].append(f"invalid append_digest byte 0x{hdr[23]:02X} at 0x{offset:x}")
    if not 0 < segment_count <= 16:
        out["problems"].append(f"implausible segment count {segment_count}")
        return out

    segs = []
    pos = offset + IMAGE_HEADER_LEN
    for i in range(segment_count):
        if pos + SEG_HEADER_LEN > len(data):
            out["problems"].append(f"segment {i} header runs past the end of the dump")
            return out
        addr, size = struct.unpack("<II", data[pos:pos + SEG_HEADER_LEN])
        pos += SEG_HEADER_LEN
        if size == 0 or size >= 0x1000000 or size % 4:
            out["problems"].append(f"segment {i} has an invalid length 0x{size:x}")
            return out
        if pos + size > len(data):
            out["problems"].append(f"segment {i} data runs past the end of the dump "
                                   f"(0x{pos + size - offset:x} > dump)")
            return out
        segs.append((addr, size))
        pos += size
    out["segments"] = segs

    body_end = align16(pos - offset)
    out["body_len"] = body_end
    if out["append_digest"]:
        # The digest covers the image body and sits immediately after it (the exact byte the padding
        # ends on differs between esptool versions, so find it rather than assume it).
        for cand in range(max(IMAGE_HEADER_LEN, body_end - 16), body_end + 65):
            start = offset + cand
            if start + 32 > len(data):
                break
            if hashlib.sha256(data[offset:start]).digest() == data[start:start + 32]:
                out["digest_at"] = cand
                out["digest_ok"] = True
                out["total_len"] = cand + 32
                break
        else:
            out["digest_ok"] = False
            out["problems"].append(
                f"appended SHA-256 does not verify: no offset in "
                f"0x{max(0, body_end - 4):x}..0x{body_end + 32:x} matches the digest of the image body")
            out["total_len"] = body_end
    else:
        out["digest_ok"] = None
        out["total_len"] = body_end
    out["ok"] = not out["problems"]
    return out


def parse_partition_table(data: bytes, offset: int = 0x8000) -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    problems: list[str] = []
    for i in range(16):
        raw = data[offset + 32 * i:offset + 32 * (i + 1)]
        if len(raw) < 32:
            problems.append(f"partition table ends inside entry {i}")
            break
        magic, typ, sub, off, size = struct.unpack("<HBBII", raw[:12])
        if magic == PART_MAGIC_END:
            break
        if magic != PART_MAGIC:
            problems.append(f"partition entry {i} has a bad magic 0x{magic:04x} at 0x{offset + 32 * i:x}")
            break
        entries.append({
            "type": typ, "subtype": sub, "offset": off, "size": size,
            "label": raw[12:28].split(b"\x00")[0].decode("utf-8", "replace"),
        })
    if not entries:
        problems.append(f"no partition entries at 0x{offset:x}")
    return entries, problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--expected-size", default="0x800000")
    ap.add_argument("--chip", default="esp32s3", choices=sorted(CHIPS))
    ap.add_argument("--partition-table-offset", default="0x8000")
    ap.add_argument("--allow-missing-digest", action="store_true",
                    help="downgrade image-digest problems to warnings (emergency restore only)")
    a = ap.parse_args()

    want = int(a.expected_size, 16)
    size = os.path.getsize(a.dump)
    data = open(a.dump, "rb").read()
    chip_id, boot_off = CHIPS[a.chip]
    pt_off = int(a.partition_table_offset, 16)
    failures: list[str] = []
    warnings: list[str] = []

    if size != want:
        failures.append(f"size {size} bytes != expected {want}")
    if size < pt_off + 32:
        failures.append(f"dump is too small to hold a partition table at 0x{pt_off:x}")
        print(f"{a.dump}: {size} bytes")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"{a.dump}: {size} bytes, chip {a.chip} (image chip id {chip_id})")

    # 1. bootloader
    boot = parse_image(data, boot_off, chip_id)
    print(f"  bootloader    @0x{boot_off:04x}: magic 0x{boot.get('magic', 0):02X}, "
          f"segments {boot.get('segment_count')}, entry 0x{boot.get('entry', 0):08x}, "
          f"chip id {boot.get('chip_id')}, digest {boot.get('digest_ok')}")
    _triage(boot, "bootloader", failures, warnings, a.allow_missing_digest)

    # 2. partition table
    entries, pt_problems = parse_partition_table(data, pt_off)
    for p in pt_problems:
        failures.append(p)
    print(f"  partition tbl @0x{pt_off:04x}: {len(entries)} entries, "
          f"magic 0x{struct.unpack('<H', data[pt_off:pt_off + 2])[0]:04x}")
    for e in entries:
        kind = PART_TYPES.get(e["type"], f"type{e['type']}")
        sub = PART_SUBTYPES.get((e["type"], e["subtype"]), f"0x{e['subtype']:02x}")
        if e["offset"] + e["size"] > size:
            failures.append(f"partition '{e['label']}' at 0x{e['offset']:x}+0x{e['size']:x} "
                            f"runs past the end of the dump")
        print(f"    {kind}/{sub:9s} off=0x{e['offset']:06x} size=0x{e['size']:06x} "
              f"({e['size'] / 1024:.0f}K) label={e['label']}")
    labels = [e["label"] for e in entries]
    if len(set(labels)) != len(labels):
        failures.append(f"duplicate partition labels: {labels}")
    apps = [e for e in entries if e["type"] == 0]
    if not apps:
        failures.append("partition table has no app partition")

    # 3. every app partition must actually hold an image for this chip
    for e in apps:
        img = parse_image(data, e["offset"], chip_id)
        print(f"  app image     @0x{e['offset']:06x} ('{e['label']}'): magic 0x{img.get('magic', 0):02X}, "
              f"segments {img.get('segment_count')}, entry 0x{img.get('entry', 0):08x}, "
              f"chip id {img.get('chip_id')}, digest {img.get('digest_ok')}, "
              f"length 0x{img.get('total_len', 0):x} of 0x{e['size']:x}")
        _triage(img, f"app '{e['label']}'", failures, warnings, a.allow_missing_digest)

    for w in warnings:
        print(f"  warn  {w}")
    if failures:
        print("FAIL: dump is not a bootable image:")
        for f in failures:
            print(f"  - {f}")
        print("Do NOT flash over this device until a good dump exists.")
        return 1
    print("ok: dump has a valid bootloader, partition table and application image")
    return 0


def _triage(img: dict, what: str, failures: list[str], warnings: list[str],
            allow_missing_digest: bool) -> None:
    for p in img["problems"]:
        target = warnings if (allow_missing_digest and "SHA-256 does not verify" in p) else failures
        target.append(f"{what}: {p}")


if __name__ == "__main__":
    sys.exit(main())
