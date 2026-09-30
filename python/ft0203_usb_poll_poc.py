#!/usr/bin/env python3
"""
Active USB polling PoC for Cotexh FT-0203 (Tenx 1130:0829).

This follows the same command pattern used by WH1080-family software:
- Send an 8-byte read command via control transfer
- Receive 32 bytes from interrupt IN endpoint 0x81

If your station is protocol-compatible, this will dump readable memory blocks.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time

import usb.core
import usb.util


READ_COMMAND = 0xA1
END_MARK = 0x20


def hexdump_line(address: int, data: list[int]) -> str:
    hex_part = " ".join(f"{b:02x}" for b in data)
    ascii_part = "".join(chr(b) if 32 <= b <= 126 else "." for b in data)
    return f"{address:04x}  {hex_part}  |{ascii_part}|"


class StationUSB:
    def __init__(self, vid: int, pid: int) -> None:
        self.dev = usb.core.find(idVendor=vid, idProduct=pid)
        if self.dev is None:
            raise RuntimeError(f"Device {vid:04x}:{pid:04x} not found")

        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass

        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, 0)
        self.in_ep, self.out_ep, self.packet_size = self._find_interrupt_endpoints()

    def _find_interrupt_endpoints(self) -> tuple[int, int, int]:
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        in_ep = None
        out_ep = None
        packet_size = 32
        for ep in intf.endpoints():
            if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_INTR:
                continue
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                in_ep = ep.bEndpointAddress
                packet_size = ep.wMaxPacketSize
            else:
                out_ep = ep.bEndpointAddress
        if in_ep is None or out_ep is None:
            raise RuntimeError("Missing interrupt IN/OUT endpoint")
        return in_ep, out_ep, packet_size

    def close(self) -> None:
        try:
            usb.util.release_interface(self.dev, 0)
        except usb.core.USBError:
            pass

    def read_block(self, address: int) -> list[int]:
        cmd = [
            READ_COMMAND,
            (address >> 8) & 0xFF,
            address & 0xFF,
            END_MARK,
            READ_COMMAND,
            (address >> 8) & 0xFF,
            address & 0xFF,
            END_MARK,
        ]

        bm_request_type = usb.util.build_request_type(
            usb.util.ENDPOINT_OUT,
            usb.util.CTRL_TYPE_CLASS,
            usb.util.CTRL_RECIPIENT_INTERFACE,
        )
        written = self.dev.ctrl_transfer(
            bmRequestType=bm_request_type,
            bRequest=usb.REQ_SET_CONFIGURATION,
            wValue=0x200,
            data_or_wLength=cmd,
            timeout=200,
        )
        if written != len(cmd):
            raise RuntimeError(f"Short write: {written} of {len(cmd)}")

        try:
            data = self.dev.read(self.in_ep, self.packet_size, timeout=800)
            return list(data)
        except usb.core.USBTimeoutError:
            pass

        out_buf = cmd + [0] * max(0, self.packet_size - len(cmd))
        sent = self.dev.write(self.out_ep, out_buf, timeout=200)
        if sent <= 0:
            raise RuntimeError("Interrupt OUT write failed")

        data = self.dev.read(self.in_ep, self.packet_size, timeout=1500)
        if len(data) == 0:
            raise RuntimeError("Empty read")
        return list(data)


def parse_int(s: str) -> int:
    return int(s, 0)


def iso_utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


def default_capture_path(start_ts: float) -> str:
    stamp = dt.datetime.fromtimestamp(start_ts, tz=dt.timezone.utc).strftime(
        "%Y%m%d_%H%M%SZ"
    )
    return str(pathlib.Path(f"ft0203_capture_{stamp}.ndjson"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll FT-0203 USB memory blocks")
    parser.add_argument("--vid", type=parse_int, default=0x1130, help="USB vendor ID")
    parser.add_argument("--pid", type=parse_int, default=0x0829, help="USB product ID")
    parser.add_argument("--start", type=parse_int, default=0x0000, help="start address")
    parser.add_argument("--blocks", type=int, default=8, help="number of 32-byte blocks")
    parser.add_argument(
        "--capture",
        default="",
        nargs="?",
        const="auto",
        help=(
            "enable long-running capture mode; optionally set NDJSON output "
            "path (default: auto timestamped filename)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="seconds between polling cycles in capture mode",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="capture duration in seconds; 0 means run until Ctrl+C",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="stop after N cycles in capture mode; 0 means no cycle limit",
    )
    parser.add_argument(
        "--only-changes",
        action="store_true",
        help="in capture mode, only emit records when a block payload changes",
    )
    args = parser.parse_args()

    if args.blocks <= 0:
        print("--blocks must be > 0", file=sys.stderr)
        return 2
    if args.interval <= 0:
        print("--interval must be > 0", file=sys.stderr)
        return 2
    if args.duration < 0:
        print("--duration must be >= 0", file=sys.stderr)
        return 2
    if args.max_cycles < 0:
        print("--max-cycles must be >= 0", file=sys.stderr)
        return 2

    try:
        ws = StationUSB(args.vid, args.pid)
    except Exception as e:
        print(f"Open failed: {e}", file=sys.stderr)
        return 2

    print(
        f"Using endpoints IN=0x{ws.in_ep:02x} OUT=0x{ws.out_ep:02x} packet={ws.packet_size}"
    )

    if not args.capture:
        ok = 0
        try:
            for i in range(args.blocks):
                addr = args.start + (i * 32)
                try:
                    block = ws.read_block(addr)
                except Exception as e:
                    print(f"Read failed at 0x{addr:04x}: {e}", file=sys.stderr)
                    return 3
                print(hexdump_line(addr, block))
                ok += 1
        finally:
            ws.close()

        print(f"Read {ok} block(s) successfully.")
        return 0

    start_ts = time.time()
    capture_path = (
        default_capture_path(start_ts) if args.capture == "auto" else args.capture
    )
    deadline = start_ts + args.duration if args.duration > 0 else None
    cycle = 0
    total_records = 0
    prev_by_addr: dict[int, str] = {}

    print(f"Capture mode enabled. Writing NDJSON to: {capture_path}")
    print("Stop with Ctrl+C.")

    try:
        with open(capture_path, "a", encoding="utf-8") as out:
            meta = {
                "type": "meta",
                "ts": start_ts,
                "ts_iso": iso_utc(start_ts),
                "capture_path": capture_path,
                "vid": f"0x{args.vid:04x}",
                "pid": f"0x{args.pid:04x}",
                "in_ep": f"0x{ws.in_ep:02x}",
                "out_ep": f"0x{ws.out_ep:02x}",
                "packet_size": ws.packet_size,
                "start_addr": args.start,
                "blocks": args.blocks,
                "interval_s": args.interval,
                "duration_s": args.duration,
                "max_cycles": args.max_cycles,
                "only_changes": args.only_changes,
            }
            out.write(json.dumps(meta) + "\n")
            out.flush()

            while True:
                now = time.time()
                if deadline is not None and now >= deadline:
                    break
                if args.max_cycles > 0 and cycle >= args.max_cycles:
                    break

                cycle_started = time.time()
                cycle_emitted = 0

                for i in range(args.blocks):
                    addr = args.start + (i * 32)
                    try:
                        block = ws.read_block(addr)
                    except Exception as e:
                        err_ts = time.time()
                        err = {
                            "type": "error",
                            "ts": err_ts,
                            "ts_iso": iso_utc(err_ts),
                            "cycle": cycle,
                            "address": addr,
                            "message": str(e),
                        }
                        out.write(json.dumps(err) + "\n")
                        out.flush()
                        print(f"Read failed at 0x{addr:04x}: {e}", file=sys.stderr)
                        return 3

                    hex_payload = "".join(f"{b:02x}" for b in block)
                    changed = prev_by_addr.get(addr) != hex_payload
                    prev_by_addr[addr] = hex_payload
                    if args.only_changes and not changed:
                        continue

                    ts = time.time()
                    rec = {
                        "type": "frame",
                        "ts": ts,
                        "ts_iso": iso_utc(ts),
                        "cycle": cycle,
                        "cycle_started_ts": cycle_started,
                        "address": addr,
                        "index": i,
                        "len": len(block),
                        "hex": hex_payload,
                        "changed": changed,
                    }
                    out.write(json.dumps(rec) + "\n")
                    cycle_emitted += 1

                out.flush()
                total_records += cycle_emitted
                elapsed = time.time() - cycle_started
                print(
                    f"cycle={cycle} emitted={cycle_emitted} elapsed={elapsed:.3f}s total={total_records}"
                )
                cycle += 1

                sleep_for = args.interval - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("Interrupted by user.")
    finally:
        ws.close()

    print(f"Capture complete: cycles={cycle}, emitted_records={total_records}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
