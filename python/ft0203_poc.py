#!/usr/bin/env python3
"""
PoC reader for Cotexh FT-0203 (Tenx 1130:0829) via hidraw.

This script currently does:
- Auto-detect the matching hidraw device from sysfs
- Read fixed-size 64-byte HID input reports
- Print timestamped hex frames
- Write newline-delimited JSON frames to a file

It is intentionally protocol-agnostic: frame decoding is left as a second step
once we capture enough samples.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import select
import sys
import time
from typing import Optional

VENDOR_HEX = "1130"
PRODUCT_HEX = "0829"
REPORT_SIZE = 64


def find_hidraw_node(vendor: str, product: str) -> Optional[str]:
    """Return /dev/hidrawX for a HID_ID match, or None if not found."""
    base = pathlib.Path("/sys/class/hidraw")
    if not base.exists():
        return None

    expected = f"hid_id=0003:0000{vendor.lower()}:0000{product.lower()}"
    for hidraw in sorted(base.glob("hidraw*")):
        uevent = hidraw / "device" / "uevent"
        try:
            text = uevent.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if expected in text.lower():
            return f"/dev/{hidraw.name}"
    return None


def read_frames(device: str, seconds: float, max_frames: int, output_path: str) -> int:
    fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    out = open(output_path, "a", encoding="utf-8")
    captured = 0
    deadline = time.time() + seconds if seconds > 0 else None

    try:
        while True:
            if deadline is not None and time.time() >= deadline:
                break
            if max_frames > 0 and captured >= max_frames:
                break

            timeout = 0.5
            r, _, _ = select.select([fd], [], [], timeout)
            if not r:
                continue

            data = os.read(fd, REPORT_SIZE)
            if not data:
                continue

            ts = time.time()
            frame_hex = data.hex()
            print(f"{ts:.3f} len={len(data):02d} hex={frame_hex}")
            out.write(json.dumps({"ts": ts, "len": len(data), "hex": frame_hex}) + "\n")
            out.flush()
            captured += 1
    finally:
        out.close()
        os.close(fd)

    return captured


def main() -> int:
    parser = argparse.ArgumentParser(description="Read raw HID frames from Cotexh FT-0203")
    parser.add_argument("--device", default="", help="hidraw device (e.g., /dev/hidraw16)")
    parser.add_argument("--seconds", type=float, default=20.0, help="capture duration; 0 means no time limit")
    parser.add_argument("--max-frames", type=int, default=200, help="stop after N frames; 0 means no frame limit")
    parser.add_argument(
        "--output",
        default="ft0203_frames.ndjson",
        help="output newline-delimited JSON file for captured frames",
    )
    args = parser.parse_args()

    device = args.device or find_hidraw_node(VENDOR_HEX, PRODUCT_HEX)
    if not device:
        print(
            "Could not auto-detect FT-0203 hidraw node. Pass --device /dev/hidrawX",
            file=sys.stderr,
        )
        return 2

    print(f"Using device: {device}")
    print(f"Writing frames to: {args.output}")

    try:
        n = read_frames(device, args.seconds, args.max_frames, args.output)
    except PermissionError:
        print(
            f"Permission denied on {device}. Run with sudo or add a udev rule.",
            file=sys.stderr,
        )
        return 13
    except KeyboardInterrupt:
        print("Interrupted by user.")
        return 130

    print(f"Captured {n} frame(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
