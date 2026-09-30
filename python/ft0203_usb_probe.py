#!/usr/bin/env python3
"""Record explicit HID transport experiments using the existing A1 command.

These are protocol hypotheses, not validated memory reads. Each transaction is
logged separately; no automatic transport fallback is used. Run offline with
--help to see options; a normal run requires the attached station and PyUSB.
"""

import argparse
import json
import time

import usb.core

from ft0203_usb_poll_poc import StationUSB, iso_utc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="NDJSON output (created exclusively)")
    parser.add_argument("--duration", type=float, default=60,
                        help="seconds of interrupt-only polling after transport probes")
    args = parser.parse_args()
    if args.duration < 0:
        parser.error("--duration must be >= 0")
    start = time.time()
    path = args.out or f"ft0203_usb_probe_{int(start)}.ndjson"
    with open(path, "x", encoding="utf-8") as out:
        def emit(kind: str, **values) -> None:
            ts = time.time()
            record = {"type": kind, "ts": ts, "ts_iso": iso_utc(ts), **values}
            out.write(json.dumps(record) + "\n")
            out.flush()
            print(json.dumps(record), flush=True)

        ws = StationUSB(0x1130, 0x0829)
        try:
            emit("meta", in_ep=ws.in_ep, out_ep=ws.out_ep,
                 packet_size=ws.packet_size, duration_s=args.duration,
                 command_status="unverified WH1080-style hypothesis")
            descriptor = ws.dev.ctrl_transfer(0x81, 0x06, 0x2200, 0, 39, timeout=1000)
            emit("descriptor", hex=bytes(descriptor).hex())

            def read(label: str) -> None:
                began = time.monotonic()
                try:
                    data = bytes(ws.dev.read(ws.in_ep, ws.packet_size, timeout=1000))
                    emit("response", experiment=label, hex=data.hex(), len=len(data),
                         elapsed_ms=round((time.monotonic() - began) * 1000, 3))
                except usb.core.USBError as exc:
                    emit("read_error", experiment=label, message=str(exc),
                         timeout=isinstance(exc, usb.core.USBTimeoutError),
                         elapsed_ms=round((time.monotonic() - began) * 1000, 3))

            def exchange(transport: str, address: int, size: int, label: str) -> None:
                cmd = [0xA1, address >> 8, address & 255, 0x20] * 2
                cmd += [0] * (size - len(cmd))
                emit("request", experiment=label, transport=transport,
                     address_hypothesis=address, hex=bytes(cmd).hex(),
                     setup=({"bmRequestType": 0x21, "bRequest": 0x09,
                             "wValue": 0x0200, "wIndex": 0}
                            if transport == "control" else None))
                began = time.monotonic()
                try:
                    if transport == "control":
                        sent = ws.dev.ctrl_transfer(0x21, 0x09, 0x0200, 0, cmd, timeout=1000)
                    else:
                        sent = ws.dev.write(ws.out_ep, cmd, timeout=1000)
                    emit("write_result", experiment=label, sent=sent,
                         expected=len(cmd),
                         elapsed_ms=round((time.monotonic() - began) * 1000, 3))
                except usb.core.USBError as exc:
                    emit("write_error", experiment=label, message=str(exc))
                    return
                read(label)

            # A timeout here establishes an empty queue before testing outputs.
            read("passive_before")
            emit("request", experiment="get_input_report",
                 setup={"bmRequestType": 0xA1, "bRequest": 0x01,
                        "wValue": 0x0100, "wIndex": 0, "wLength": 64})
            try:
                data = bytes(ws.dev.ctrl_transfer(0xA1, 0x01, 0x0100, 0, 64, timeout=1000))
                emit("response", experiment="get_input_report", hex=data.hex(), len=len(data))
            except usb.core.USBError as exc:
                emit("read_error", experiment="get_input_report", message=str(exc))

            for transport, size in [("control", 8), ("control", 64), ("interrupt", 64)]:
                for address in [0, 0x20, 0x40, 0x100]:
                    label = f"{transport}_{size}_addr_{address:04x}"
                    exchange(transport, address, size, label)
                    read(label + "_extra")

            deadline = time.monotonic() + args.duration
            cycle = 0
            while time.monotonic() < deadline:
                exchange("interrupt", 0, 64, f"time_series_{cycle}")
                cycle += 1
                time.sleep(2)
            read("passive_after")
            emit("complete", cycles=cycle, path=path)
        finally:
            ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
