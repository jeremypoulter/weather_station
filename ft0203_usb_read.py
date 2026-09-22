#!/usr/bin/env python3
"""Read FT0203A current weather packets and display candidate field decodes.

The documented WeatherHome commands and packet framing are verified on this
station. Only indoor/outdoor temperature and humidity have display validation;
all other weather values are intentionally labelled as provisional.
"""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import usb.core

from ft0203_usb_poll_poc import StationUSB, iso_utc


INFO_COMMAND = 0x01
CURRENT_COMMAND = 0x04
REPORT_SIZE = 64
CURRENT_PACKET_LENGTH = 76
DEFAULT_INTERVAL_SECONDS = 16.0


def validate_packet(data: bytes) -> bytes:
    if not data or not 3 <= data[0] <= 128:
        raise ValueError("Invalid packet length")
    length = data[0]
    if len(data) < length:
        raise ValueError(f"Truncated packet: {len(data)} < {length}")
    packet = data[:length]
    checksum = sum(packet[:-1]) & 0xFF
    if checksum != packet[-1]:
        raise ValueError(f"Checksum mismatch: {checksum:02x} != {packet[-1]:02x}")
    return packet


def le16(packet: bytes, offset: int) -> int:
    return packet[offset] | (packet[offset + 1] << 8)


def fahrenheit_tenths_to_celsius(raw: int) -> float:
    return ((raw - 400) / 10.0 - 32.0) * 5.0 / 9.0


def decode_rain(raw: int, nulls: set[int], packed: bool = False) -> float | None:
    if raw in nulls or raw & 0x0FFF == 0x0FFF:
        return None
    return (raw >> 4 if packed else raw) * 0.1


def dew_point_celsius(temp_c: float, humidity: int) -> float:
    alpha = 17.625 * temp_c / (243.04 + temp_c) + math.log(humidity / 100.0)
    return 243.04 * alpha / (17.625 - alpha)


def feels_like_celsius(temp_c: float, humidity: int, wind_m_s: float) -> float:
    if temp_c <= 10.0 and wind_m_s > 1.3:
        wind_km_h = wind_m_s * 3.6
        return (
            13.12
            + 0.6215 * temp_c
            - 11.37 * wind_km_h**0.16
            + 0.3965 * temp_c * wind_km_h**0.16
        )
    if temp_c >= 26.7:
        temp_f = temp_c * 9.0 / 5.0 + 32.0
        heat_index_f = 0.5 * (temp_f + 61.0 + (temp_f - 68.0) * 1.2 + humidity * 0.094)
        if heat_index_f >= 80.0:
            heat_index_f = (
                -42.379
                + 2.04901523 * temp_f
                + 10.14333127 * humidity
                - 0.22475541 * temp_f * humidity
                - 0.00683783 * temp_f**2
                - 0.05481717 * humidity**2
                + 0.00122874 * temp_f**2 * humidity
                + 0.00085282 * temp_f * humidity**2
                - 0.00000199 * temp_f**2 * humidity**2
            )
        return (heat_index_f - 32.0) * 5.0 / 9.0
    return temp_c


def field(value: float | int | None, unit: str, status: str, raw: int | None = None) -> dict[str, Any]:
    return {"value": value, "unit": unit, "status": status, "raw": raw}


def decode_current_packet(packet: bytes) -> dict[str, Any]:
    """Decode confirmed values and clearly labelled reference candidates."""
    if len(packet) != CURRENT_PACKET_LENGTH or packet[1] != CURRENT_COMMAND:
        raise ValueError("Not a complete current-reading packet")

    indoor_raw = le16(packet, 0x07) & 0x0FFF
    outdoor_raw = le16(packet, 0x0A) & 0x0FFF
    indoor_temp = fahrenheit_tenths_to_celsius(indoor_raw)
    outdoor_temp = fahrenheit_tenths_to_celsius(outdoor_raw)
    indoor_humidity = packet[0x09]
    outdoor_humidity = packet[0x16]
    wind_average_raw = packet[0x3A]
    wind_average = wind_average_raw * 0.1
    gust_raw = le16(packet, 0x3B)
    direction_raw = le16(packet, 0x3D)
    fields = {
        "outdoor_temperature": field(outdoor_temp, "C", "validated", outdoor_raw),
        "outdoor_humidity": field(outdoor_humidity, "%", "validated", outdoor_humidity),
        "indoor_temperature": field(indoor_temp, "C", "validated", indoor_raw),
        "indoor_humidity": field(indoor_humidity, "%", "validated", indoor_humidity),
        "absolute_pressure": field(le16(packet, 0x36) * 0.1, "hPa", "validated", le16(packet, 0x36)),
        "relative_pressure": field(le16(packet, 0x38) * 0.1, "hPa", "validated", le16(packet, 0x38)),
        "wind_average": field(wind_average, "m/s", "validated", wind_average_raw),
        "wind_gust": field(gust_raw * 0.00625, "m/s", "validated", gust_raw),
        "wind_direction": field(direction_raw, "degrees", "validated", direction_raw),
        "rain_last_hour": field(decode_rain(le16(packet, 0x3F), {0xA7FA}), "mm", "validated", le16(packet, 0x3F)),
        "rain_today": field(decode_rain(le16(packet, 0x41), {0x7FA7}), "mm", "validated", le16(packet, 0x41)),
        "rain_week": field(decode_rain(le16(packet, 0x43), {0xFAA7}), "mm", "validated", le16(packet, 0x43)),
        "rain_month": field(decode_rain(le16(packet, 0x45), set(), packed=True), "mm", "validated", le16(packet, 0x45)),
        "rain_total": field(le16(packet, 0x48) * 0.1, "mm", "validated", le16(packet, 0x48)),
        "dew_point": field(dew_point_celsius(outdoor_temp, outdoor_humidity), "C", "derived_validated"),
        "feels_like": field(feels_like_celsius(outdoor_temp, outdoor_humidity, wind_average), "C", "derived_validated"),
    }
    return {
        "packet_length": len(packet),
        "response_code": packet[1],
        "header_hex": packet[:4].hex(),
        "tracker_raw": le16(packet, 0x4A),
        "fields": fields,
    }


def value_text(entry: dict[str, Any], precision: int = 1) -> str:
    value = entry["value"]
    if value is None:
        return "unavailable"
    if isinstance(value, int):
        return f"{value} {entry['unit']}"
    return f"{value:.{precision}f} {entry['unit']}"


def plain_reading(reading: dict[str, Any]) -> str:
    fields = reading["fields"]
    return " | ".join(
        [
            f"out {value_text(fields['outdoor_temperature'])}, {value_text(fields['outdoor_humidity'], 0)}",
            f"in {value_text(fields['indoor_temperature'])}, {value_text(fields['indoor_humidity'], 0)}",
            f"pressure {value_text(fields['relative_pressure'])}",
            f"gust {value_text(fields['wind_gust'])}",
            f"direction {value_text(fields['wind_direction'], 0)}",
            f"wind? {value_text(fields['wind_average'])}",
        ]
    )


class Dashboard:
    def __init__(self, screen: Any) -> None:
        self.screen = screen
        self.last_reading: dict[str, Any] | None = None
        self.last_packet = ""
        self.changed_offsets: list[int] = []
        self.error = ""
        self.samples = 0
        self.errors = 0
        self.output = ""
        self.last_success = 0.0
        self.raw_visible = False
        self.running = True
        self.screen.nodelay(True)
        curses.curs_set(0)

    def update(self, reading: dict[str, Any], packet: bytes, changed_offsets: list[int]) -> None:
        self.last_reading = reading
        self.last_packet = packet.hex()
        self.changed_offsets = changed_offsets
        self.samples += 1
        self.last_success = time.time()
        self.error = ""

    def set_error(self, message: str) -> None:
        self.errors += 1
        self.error = message

    def draw(self) -> None:
        self.screen.erase()
        rows, cols = self.screen.getmaxyx()
        if rows < 21 or cols < 72:
            self.screen.addnstr(0, 0, "Terminal needs at least 72 columns and 21 rows.", max(1, cols - 1))
            self.screen.refresh()
            return
        age = "never" if not self.last_success else f"{time.time() - self.last_success:.1f}s ago"
        self.screen.addstr(0, 0, "FT-0203 Weather Station  |  Ctrl+C quit  r raw packet  h help", curses.A_BOLD)
        self.screen.addstr(1, 0, f"USB: connected  Last valid: {age}  Samples: {self.samples}  Errors: {self.errors}")
        if self.error:
            self.screen.addnstr(2, 0, f"Last error: {self.error}", cols - 1, curses.A_BOLD)
        if self.last_reading is None:
            self.screen.addstr(4, 0, "Waiting for the first validated current-reading packet...")
            self.screen.refresh()
            return
        fields = self.last_reading["fields"]
        self.screen.addstr(4, 0, "Validated readings", curses.A_BOLD)
        self._row(5, "Outdoor temperature", value_text(fields["outdoor_temperature"]), "Outdoor humidity", value_text(fields["outdoor_humidity"], 0))
        self._row(6, "Indoor temperature", value_text(fields["indoor_temperature"]), "Indoor humidity", value_text(fields["indoor_humidity"], 0))
        self._row(7, "Relative pressure", value_text(fields["relative_pressure"]), "Dew point", value_text(fields["dew_point"]))
        self._row(8, "Absolute pressure", value_text(fields["absolute_pressure"]), "Feels like", value_text(fields["feels_like"]))
        self._row(9, "Wind gust", value_text(fields["wind_gust"]), "Wind direction", value_text(fields["wind_direction"], 0))
        self._row(10, "Wind average", value_text(fields["wind_average"]), "", "")
        self._row(12, "Rain last hour", value_text(fields["rain_last_hour"]), "Rain today", value_text(fields["rain_today"]))
        self._row(13, "Rain week", value_text(fields["rain_week"]), "Rain month", value_text(fields["rain_month"]))
        self._row(14, "Rain total", value_text(fields["rain_total"]), "", "")
        self.screen.addstr(18, 0, f"Header: {self.last_reading['header_hex']}  Tracker: 0x{self.last_reading['tracker_raw']:04x}")
        changes = ", ".join(f"0x{offset:02x}" for offset in self.changed_offsets) or "none"
        self.screen.addnstr(19, 0, f"Changed bytes: {changes}  Output: {self.output}", cols - 1)
        if self.raw_visible:
            self.screen.addnstr(20, 0, f"Packet: {self.last_packet}", cols - 1)
        self.screen.refresh()

    def _row(self, row: int, left_name: str, left_value: str, right_name: str, right_value: str) -> None:
        self.screen.addstr(row, 2, f"{left_name:<22} {left_value:<16}")
        self.screen.addstr(row, 43, f"{right_name:<20} {right_value}")

    def handle_keys(self) -> None:
        key = self.screen.getch()
        if key in (ord("r"), ord("R")):
            self.raw_visible = not self.raw_visible
        elif key in (ord("h"), ord("H")):
            self.error = "r toggles raw packet; Ctrl+C exits. Values marked provisional/conflicting need display checks."
        elif key in (ord("q"), ord("Q")):
            self.running = False


def default_capture_path(start_ts: float) -> str:
    stamp = dt.datetime.fromtimestamp(start_ts, tz=dt.timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    return f"ft0203_usb_read_{stamp}.ndjson"


def run_reader(args: argparse.Namespace, dashboard: Dashboard | None = None) -> int:
    start = time.time()
    path = args.out or default_capture_path(start)
    deadline = start + args.duration if args.duration else None
    failures = 0
    previous_packet: bytes | None = None
    with open(path, "x", encoding="utf-8") as out:
        def emit(kind: str, **values: Any) -> None:
            ts = time.time()
            record = {"type": kind, "ts": ts, "ts_iso": iso_utc(ts), **values}
            out.write(json.dumps(record) + "\n")
            out.flush()

        ws = StationUSB(0x1130, 0x0829)
        try:
            emit("meta", in_ep=ws.in_ep, out_ep=ws.out_ep, packet_size=ws.packet_size,
                 samples=args.samples, interval_s=args.interval, duration_s=args.duration,
                 source="WeatherHome protocol verified on FT0203")
            if ws.packet_size != REPORT_SIZE:
                raise ValueError(f"Unexpected USB report size: {ws.packet_size}")
            for _ in range(8):
                try:
                    pending = bytes(ws.dev.read(ws.in_ep, REPORT_SIZE, timeout=200))
                    emit("pending_report", hex=pending.hex())
                except usb.core.USBTimeoutError:
                    break
            else:
                raise ValueError("Input queue did not become idle")

            def exchange(command: int, sample: int | None) -> tuple[bytes, dict[str, Any] | None]:
                request = bytes([3, command, (3 + command) & 0xFF]) + bytes(REPORT_SIZE - 3)
                emit("request", command=command, sample=sample, hex=request.hex())
                sent = ws.dev.write(ws.out_ep, request, timeout=1000)
                if sent != REPORT_SIZE:
                    raise ValueError(f"Short write: {sent}/{REPORT_SIZE}")
                data = b""
                while True:
                    report = bytes(ws.dev.read(ws.in_ep, REPORT_SIZE, timeout=2000))
                    emit("report", command=command, sample=sample, len=len(report), hex=report.hex())
                    if not report:
                        raise ValueError("Empty USB report")
                    data += report
                    if not 3 <= data[0] <= 128:
                        raise ValueError(f"Invalid declared length: {data[0]}")
                    if len(data) >= data[0]:
                        break
                packet = validate_packet(data)
                if packet[1] != command:
                    raise ValueError(f"Unexpected response code: {packet[1]:02x}")
                emit("packet", command=command, sample=sample, len=len(packet), hex=packet.hex(),
                     checksum_valid=True, response_code=packet[1], response_matches=True)
                return packet, decode_current_packet(packet) if command == CURRENT_COMMAND else None

            info, _ = exchange(INFO_COMMAND, None)
            emit("device_info", hex=info.hex())
            sample = 0
            while not args.samples or sample < args.samples:
                if deadline is not None and time.time() >= deadline:
                    break
                try:
                    packet, reading = exchange(CURRENT_COMMAND, sample)
                    assert reading is not None
                    changed_offsets = [] if previous_packet is None else [
                        index for index, (old, new) in enumerate(zip(previous_packet, packet)) if old != new
                    ]
                    emit("reading", sample=sample, packet_hex=packet.hex(),
                         changed_offsets=changed_offsets, **reading)
                    if dashboard:
                        dashboard.update(reading, packet, changed_offsets)
                    else:
                        print(f"{iso_utc(time.time())}  {plain_reading(reading)}", flush=True)
                    previous_packet = packet
                    sample += 1
                except (usb.core.USBError, ValueError) as exc:
                    failures += 1
                    emit("error", command=CURRENT_COMMAND, sample=sample, message=str(exc))
                    if dashboard:
                        dashboard.set_error(str(exc))
                    else:
                        print(f"USB error: {exc}", file=sys.stderr, flush=True)
                if dashboard:
                    dashboard.draw()
                    dashboard.handle_keys()
                    if not dashboard.running:
                        break
                sleep_until = time.monotonic() + args.interval
                while time.monotonic() < sleep_until:
                    if dashboard:
                        dashboard.draw()
                        dashboard.handle_keys()
                        if not dashboard.running:
                            break
                    time.sleep(0.1)
                if dashboard and not dashboard.running:
                    break
            emit("complete", failures=failures, samples=sample, path=path)
        except KeyboardInterrupt:
            emit("interrupted", failures=failures)
        finally:
            ws.close()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="NDJSON output, created exclusively")
    parser.add_argument("--samples", type=int, default=0, help="stop after N current packets; 0 runs until Ctrl+C")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after N seconds; 0 runs until Ctrl+C")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="seconds between current-packet requests")
    parser.add_argument("--plain", action="store_true", help="write one human-readable line per packet instead of the TUI")
    args = parser.parse_args()
    if args.samples < 0 or args.duration < 0 or not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("samples/duration must be >= 0 and interval must be finite and > 0")
    use_tui = not args.plain and sys.stdout.isatty()
    if not use_tui:
        return run_reader(args)
    dashboard: Dashboard | None = None

    def run_dashboard(screen: Any) -> int:
        nonlocal dashboard
        dashboard = Dashboard(screen)
        dashboard.output = Path(args.out or default_capture_path(time.time())).name
        return run_reader(args, dashboard)

    return curses.wrapper(run_dashboard)


if __name__ == "__main__":
    raise SystemExit(main())
