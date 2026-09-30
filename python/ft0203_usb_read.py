#!/usr/bin/env python3
"""Read FT0203A current weather packets and display candidate field decodes.

The documented WeatherHome commands and packet framing are verified on this
station. Readings are decoded for the indoor sensor and thermometer/hygrometer
channels CH1-CH8; each field records its validation status.
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
UNKNOWN_RANGES = (
    (0x02, 0x06),
)
UNKNOWN_OFFSETS = frozenset(
    offset for start, end in UNKNOWN_RANGES for offset in range(start, end + 1)
) | frozenset({0x4A})
# Byte 0x04 sensor flags, from WeatherHome ID0040.dll ReadMainRecord: each set bit
# makes the app read that sensor's history graph; bit 6 also gates the rain fields.
SENSOR_MASK_BITS = {
    0: "in-T",
    1: "in-H",
    2: "wind",
    3: "gust",
    5: "press",
    6: "rain",
}
INVALID_12BIT = 0x7FA    # temperature/wind values >= this mean "no data"
INVALID_HUMIDITY = 0x7A  # humidity bytes >= this mean "no data"
INVALID_PRESSURE = 0x7FFA
# Nibble-packed 12-bit slots for extra thermometer channels (2-8) and their
# humidity bytes. They read 0x7fa / 0x7a ("no sensor") on this station.
CHANNEL_SLOT_RANGES = (
    (0x0C, 0x15),
    (0x17, 0x1D),
    (0x1F, 0x29),
    (0x2B, 0x35),
)


def packed12(packet: bytes, offset: int) -> int:
    """First 12-bit value of a nibble-packed block (same layout as temperatures)."""
    return le16(packet, offset) & 0x0FFF


def packed12_slot(packet: bytes, base: int, index: int) -> int:
    """12-bit value `index` of a nibble-packed block: slots share bytes in pairs."""
    offset = base + index * 3 // 2
    raw = le16(packet, offset)
    return raw >> 4 if index % 2 else raw & 0x0FFF


CHANNEL_COUNT = 8
CHANNEL_TEMPERATURE_BASE = 0x0A
CHANNEL_HUMIDITY_BASE = 0x16
CHANNEL_DEW_POINT_BASE = 0x1E
CHANNEL_FEELS_LIKE_BASE = 0x2A


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


def le32(packet: bytes, offset: int) -> int:
    return int.from_bytes(packet[offset:offset + 4], "little")


def temp12(raw: int) -> float | None:
    return None if raw >= INVALID_12BIT else fahrenheit_tenths_to_celsius(raw)


def humidity(raw: int) -> int | None:
    return None if raw >= INVALID_HUMIDITY else raw


def tenths(raw: int, invalid: int | None = None) -> float | None:
    return None if invalid is not None and raw >= invalid else raw * 0.1


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


def unknown_packet_fields(packet: bytes) -> tuple[dict[str, int], dict[str, str]]:
    """Return every unassigned byte and compact display ranges for long runs."""
    unknown_bytes = {f"0x{offset:02x}": packet[offset] for offset in sorted(UNKNOWN_OFFSETS)}
    unknown_ranges = {
        f"0x{start:02x}-0x{end:02x}": packet[start:end + 1].hex()
        for start, end in UNKNOWN_RANGES
    }
    return unknown_bytes, unknown_ranges


def decode_current_packet(packet: bytes) -> dict[str, Any]:
    """Decode confirmed values and clearly labelled reference candidates."""
    if len(packet) != CURRENT_PACKET_LENGTH or packet[1] != CURRENT_COMMAND:
        raise ValueError("Not a complete current-reading packet")

    indoor_raw = le16(packet, 0x07) & 0x0FFF
    indoor_temp = temp12(indoor_raw)
    indoor_humidity = humidity(packet[0x09])
    # Field widths follow WeatherHome's ID0040.dll parser: 12-bit wind values
    # nibble-packed from 0x3a, and 20-bit week/month/total rain counters.
    wind_average_raw = le16(packet, 0x3A) & 0x0FFF
    wind_average = tenths(wind_average_raw, INVALID_12BIT)
    gust_raw = le16(packet, 0x3B) >> 4
    direction_raw = le16(packet, 0x3D) & 0x0FFF
    rain_present = bool(packet[0x04] & 0x40)
    rain_raw = {
        "rain_last_hour": le16(packet, 0x3F),
        "rain_today": le16(packet, 0x41),
        "rain_week": le32(packet, 0x43) & 0xFFFFF,
        "rain_month": (le32(packet, 0x45) >> 4) & 0xFFFFF,
        "rain_total": le32(packet, 0x48) & 0xFFFFF,
    }
    ch1_temp = temp12(packed12_slot(packet, CHANNEL_TEMPERATURE_BASE, 0))
    ch1_humidity = humidity(packet[CHANNEL_HUMIDITY_BASE])
    ch1_ok = ch1_temp is not None and ch1_humidity is not None
    channel_fields: dict[str, dict[str, Any]] = {}
    for index in range(CHANNEL_COUNT):
        channel = index + 1
        # CH1 temperature/humidity were display-validated; the other channels
        # use the same decoding but have no sensor attached on this station.
        status = "validated" if channel == 1 else "decoded"
        temp_raw = packed12_slot(packet, CHANNEL_TEMPERATURE_BASE, index)
        humidity_raw = packet[CHANNEL_HUMIDITY_BASE + index]
        dew_raw = packed12_slot(packet, CHANNEL_DEW_POINT_BASE, index)
        feels_raw = packed12_slot(packet, CHANNEL_FEELS_LIKE_BASE, index)
        channel_fields[f"ch{channel}_temperature"] = field(temp12(temp_raw), "C", status, temp_raw)
        channel_fields[f"ch{channel}_humidity"] = field(humidity(humidity_raw), "%", status, humidity_raw)
        channel_fields[f"ch{channel}_dew_point"] = field(temp12(dew_raw), "C", "decoded", dew_raw)
        channel_fields[f"ch{channel}_feels_like"] = field(temp12(feels_raw), "C", "decoded", feels_raw)
    fields = {
        **channel_fields,
        "indoor_temperature": field(indoor_temp, "C", "validated", indoor_raw),
        "indoor_humidity": field(indoor_humidity, "%", "validated", packet[0x09]),
        "absolute_pressure": field(tenths(le16(packet, 0x36), INVALID_PRESSURE), "hPa", "validated", le16(packet, 0x36)),
        "relative_pressure": field(tenths(le16(packet, 0x38), INVALID_PRESSURE), "hPa", "validated", le16(packet, 0x38)),
        "wind_average": field(wind_average, "m/s", "validated", wind_average_raw),
        "wind_gust": field(tenths(gust_raw, INVALID_12BIT), "m/s", "validated", gust_raw),
        "wind_direction": field(None if direction_raw >= INVALID_12BIT else direction_raw, "degrees", "validated", direction_raw),
        **{
            name: field(raw * 0.1 if rain_present else None, "mm", "validated", raw)
            for name, raw in rain_raw.items()
        },
        # Calculated from CH1 as a cross-check of the console's own CH1 values.
        "dew_point": field(dew_point_celsius(ch1_temp, ch1_humidity) if ch1_ok else None, "C", "derived_validated"),
        "feels_like": field(
            feels_like_celsius(ch1_temp, ch1_humidity, wind_average or 0.0) if ch1_ok else None,
            "C", "derived_validated"),
        "sensor_mask": field(packet[0x04], "", "decoded", packet[0x04]),
        "channel_temperature_flags": field(packet[0x02], "", "decoded", packet[0x02]),
        "channel_humidity_flags": field(packet[0x03], "", "decoded", packet[0x03]),
    }
    unknown_bytes, unknown_ranges = unknown_packet_fields(packet)
    return {
        "packet_length": len(packet),
        "response_code": packet[1],
        "header_hex": packet[:4].hex(),
        "tracker_raw": packet[0x4A],
        "unknown_bytes": unknown_bytes,
        "unknown_ranges": unknown_ranges,
        "channel_slots": {
            f"0x{start:02x}-0x{end:02x}": packet[start:end + 1].hex()
            for start, end in CHANNEL_SLOT_RANGES
        },
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
    channels = [
        f"ch{channel} {value_text(fields[f'ch{channel}_temperature'])}, {value_text(fields[f'ch{channel}_humidity'], 0)}"
        for channel in range(1, CHANNEL_COUNT + 1)
        if fields[f"ch{channel}_temperature"]["value"] is not None or fields[f"ch{channel}_humidity"]["value"] is not None
    ]
    return " | ".join(
        [
            *channels,
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
        self.previous_reading: dict[str, Any] | None = None
        self.screen.nodelay(True)
        curses.curs_set(0)
        self.up_attr = self.down_attr = self.changed_attr = curses.A_BOLD
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_GREEN, background)
            curses.init_pair(2, curses.COLOR_RED, background)
            curses.init_pair(3, curses.COLOR_YELLOW, background)
            self.up_attr = curses.color_pair(1) | curses.A_BOLD
            self.down_attr = curses.color_pair(2) | curses.A_BOLD
            self.changed_attr = curses.color_pair(3) | curses.A_BOLD

    def update(self, reading: dict[str, Any], packet: bytes, changed_offsets: list[int]) -> None:
        self.previous_reading = self.last_reading
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
        if rows < 23 or cols < 82:
            self.screen.addnstr(0, 0, "Terminal needs at least 82 columns and 23 rows.", max(1, cols - 1))
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
        self.screen.addstr(4, 0, "Readings  (green = increased, red = decreased since previous packet)", curses.A_BOLD)
        self._row(5, "Relative pressure", "relative_pressure", "Absolute pressure", "absolute_pressure")
        self._row(6, "Wind average", "wind_average", "Wind gust", "wind_gust")
        self._row(7, "Wind direction", "wind_direction", "Rain last hour", "rain_last_hour", 0, 1)
        self._row(8, "Rain today", "rain_today", "Rain week", "rain_week")
        self._row(9, "Rain month", "rain_month", "Rain total", "rain_total")
        y = self._draw_sensors(11, rows)
        self._draw_status(y + 1, rows, cols)
        self.screen.refresh()

    def _draw_sensors(self, y: int, rows: int) -> int:
        """Table of indoor and CH1-CH8 sensors; channels with no data share one line."""
        assert self.last_reading is not None
        fields = self.last_reading["fields"]
        temp_flags = fields["channel_temperature_flags"]["value"]
        humidity_flags = fields["channel_humidity_flags"]["value"]
        self.screen.addstr(y, 2, f"{'Sensor':<10}{'Temperature':<14}{'Humidity':<10}{'Dew point':<12}{'Feels like':<12}Flags T/H",
                           curses.A_BOLD)
        y += 1
        self._sensor_row(y, "Indoor", "indoor_temperature", "indoor_humidity", None, None, "")
        y += 1
        absent = []
        for channel in range(1, CHANNEL_COUNT + 1):
            prefix = f"ch{channel}_"
            bit = channel - 1
            present = (temp_flags >> bit) & 1 or (humidity_flags >> bit) & 1
            has_data = any(fields[prefix + name]["value"] is not None for name in ("temperature", "humidity"))
            if not (present or has_data):
                absent.append(channel)
                continue
            if y >= rows - 1:
                break
            flags = f"{(temp_flags >> bit) & 1}/{(humidity_flags >> bit) & 1}"
            if present and not has_data:
                flags += "  no signal"
            self._sensor_row(y, f"CH{channel}", prefix + "temperature", prefix + "humidity",
                             prefix + "dew_point", prefix + "feels_like", flags)
            y += 1
        if absent and y < rows - 1:
            self.screen.addstr(y, 2, "No sensor: " + ", ".join(f"CH{channel}" for channel in absent))
            y += 1
        if y < rows - 1:
            self.screen.addstr(y, 2, "Calculated from CH1: dew point ")
            self.screen.addstr(value_text(fields["dew_point"]), self._trend_attr("dew_point"))
            self.screen.addstr(", feels like ")
            self.screen.addstr(value_text(fields["feels_like"]), self._trend_attr("feels_like"))
            y += 1
        return y

    def _sensor_row(self, y: int, name: str, temp_key: str, humidity_key: str,
                    dew_key: str | None, feels_key: str | None, flags: str) -> None:
        assert self.last_reading is not None
        fields = self.last_reading["fields"]
        self.screen.addstr(y, 2, f"{name:<10}")
        for key, width, precision in ((temp_key, 14, 1), (humidity_key, 10, 0), (dew_key, 12, 1), (feels_key, 12, 1)):
            text = "" if key is None else value_text(fields[key], precision).replace("unavailable", "--")
            self.screen.addstr(f"{text:<{width}}", curses.A_NORMAL if key is None else self._trend_attr(key))
        self.screen.addstr(flags)

    def _draw_status(self, y: int, rows: int, cols: int) -> None:
        """Header bytes still under investigation, sensor flags and diagnostics."""
        assert self.last_reading is not None
        if y + 6 + (1 if self.raw_visible else 0) > rows:
            return
        self.screen.addstr(y, 0, "Status bytes (* = range changed, yellow = changed byte)", curses.A_BOLD)
        changed_set = set(self.changed_offsets)
        y += 1
        for start, end in UNKNOWN_RANGES:
            label = f"0x{start:02x}-0x{end:02x}"
            changed = any(start <= offset <= end for offset in changed_set)
            self.screen.addstr(y, 2, f"{'*' if changed else ' '} {label}: ")
            hex_text = self.last_reading["unknown_ranges"][label]
            for position, offset in enumerate(range(start, end + 1)):
                attr = self.changed_attr if offset in changed_set else curses.A_NORMAL
                self.screen.addstr(hex_text[position * 2:position * 2 + 2], attr)
            y += 1
        self._draw_sensor_mask(y)
        y += 1
        tracker_changed = 0x4A in self.last_reading["unknown_changed_offsets"]
        self.screen.addstr(y, 0, f"Header: {self.last_reading['header_hex']}  ")
        self.screen.addstr(f"Byte 0x4a: 0x{self.last_reading['tracker_raw']:02x}{' *' if tracker_changed else ''}",
                           self.changed_attr if tracker_changed else curses.A_NORMAL)
        y += 1
        changes = ", ".join(f"0x{offset:02x}" for offset in self.changed_offsets) or "none"
        self.screen.addnstr(y, 0, f"Changed bytes: {changes}", cols - 1)
        self.screen.addnstr(y + 1, 0, f"Output: {self.output}", cols - 1)
        if self.raw_visible:
            self.screen.addnstr(y + 2, 0, f"Packet: {self.last_packet}", cols - 1)

    def _draw_sensor_mask(self, row: int) -> None:
        """Show byte 0x04 bit by bit; changed bits are yellow."""
        assert self.last_reading is not None
        mask = self.last_reading["fields"]["sensor_mask"]["value"]
        previous = None if self.previous_reading is None else self.previous_reading["fields"]["sensor_mask"]["value"]
        self.screen.addstr(row, 2, f"0x04 flags 0x{mask:02x}:", curses.A_BOLD)
        for bit in range(7, -1, -1):
            name = SENSOR_MASK_BITS.get(bit, f"b{bit}")
            value = (mask >> bit) & 1
            changed = previous is not None and ((previous >> bit) & 1) != value
            attr = self.changed_attr if changed else (curses.A_BOLD if bit in SENSOR_MASK_BITS else curses.A_NORMAL)
            self.screen.addstr(f" {name}={value}", attr)

    def _trend_attr(self, key: str) -> int:
        """Green if the value rose since the previous packet, red if it fell."""
        if self.previous_reading is None or self.last_reading is None:
            return curses.A_NORMAL
        new = self.last_reading["fields"][key]["value"]
        old = self.previous_reading["fields"][key]["value"]
        if new is None or old is None or new == old:
            return curses.A_NORMAL
        return self.up_attr if new > old else self.down_attr

    def _row(self, row: int, left_name: str, left_key: str, right_name: str, right_key: str,
             left_precision: int = 1, right_precision: int = 1) -> None:
        assert self.last_reading is not None
        fields = self.last_reading["fields"]
        self.screen.addstr(row, 2, f"{left_name:<22} ")
        self.screen.addstr(f"{value_text(fields[left_key], left_precision):<16}", self._trend_attr(left_key))
        self.screen.addstr(row, 43, f"{right_name:<20} ")
        self.screen.addstr(value_text(fields[right_key], right_precision), self._trend_attr(right_key))

    def handle_keys(self) -> None:
        key = self.screen.getch()
        if key in (ord("r"), ord("R")):
            self.raw_visible = not self.raw_visible
        elif key in (ord("h"), ord("H")):
            self.error = "r toggles raw packet; * marks unknown bytes that changed; Ctrl+C exits."
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
                    reading["unknown_changed_offsets"] = [
                        offset for offset in changed_offsets if offset in UNKNOWN_OFFSETS
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
