# FT-0203 WeatherHome USB Protocol

This document records the packet transport and current-reading layout verified
against the Cotech FT-0203 console (`1130:0829`). It covers reads only. Do not
send configuration, calibration, erase, clock, or undocumented commands.

## USB Interface

| Property | Value |
|---|---|
| USB VID:PID | `1130:0829` |
| Product string | `TMU313X USB R/W64` |
| Interface | HID, interface 0 |
| Interrupt OUT endpoint | `0x04` |
| Interrupt IN endpoint | `0x83` |
| USB report length | 64 bytes |

The HID descriptor uses a vendor-defined usage page and declares 64-byte input
and output reports. It has no report IDs and no feature reports.

The host writes a 64-byte report to `0x04`, then reads one or more 64-byte
reports from `0x83`. The Windows HID API uses a leading zero byte as an API
report-ID placeholder; that byte is **not** part of raw PyUSB transfers.

## Packet Framing

Application packets have this common structure:

```text
byte 0       Total packet length, including checksum
byte 1       Response command
byte 2..N-2  Command payload
byte N-1     Additive checksum: sum(bytes 0 through N-2) modulo 256
```

The packet length can exceed the 64-byte USB report length. Concatenate report
payloads until the declared length has been received, then ignore bytes beyond
that application packet. The current record is 76 bytes and spans two reports.

Reject packets when the declared length is outside 3-128 bytes, data is
truncated, the checksum fails, or the response command differs from the request.

## Verified Read Commands

Requests are zero-padded to 64 bytes before writing to interrupt OUT.

| Request bytes | Purpose | Verified reply |
|---|---|---|
| `03 01 04` | Device information | 11-byte command-`01` response |
| `03 04 07` | Current weather record | 76-byte command-`04` response |

The request checksum is the additive sum of its first two bytes. The device-info
packet observed during testing was:

```text
0b 01 0a 40 00 fb 73 8c 40 00 90
```

Its payload fields are not decoded.

## Current Record

The current record begins with `4c 04 01 01`, has length `0x4c` (76), and ends
at offset `0x4b` with its checksum. Offsets below include the length and command
bytes, so they are offsets into the full 76-byte application packet.

| Offset | Width | Value | Decode | Status |
|---|---:|---|---|---|
| `0x00` | 1 | packet length | Always `0x4c` for current record | verified |
| `0x01` | 1 | response command | Always `0x04` for current record | verified |
| `0x02-0x03` | 2 | header | Always `01 01` | unknown |
| `0x04` | 1 | sensor-presence mask (candidate) | `0x3f` before rain gauge registration, `0x7f` after | provisional |
| `0x05-0x06` | 2 | unknown | Always `00 00` | unknown |
| `0x07` | 2 LE | indoor temperature raw | `((raw & 0x0fff) - 400) / 10` F, then convert to C | verified |
| `0x09` | 1 | indoor humidity | percent | verified |
| `0x0a` | 1.5 | outdoor temperature (CH1) | 12-bit packed, same formula as indoor temperature | verified |
| `0x0b-0x15` | 10.5 | temperature, CH2-CH8 | seven more 12-bit packed slots; `0x7fa` = no sensor | structure inferred |
| `0x16` | 1 | outdoor humidity (CH1) | percent | verified |
| `0x17-0x1d` | 7 | humidity, CH2-CH8 | `0x7a` (122) = no sensor | structure inferred |
| `0x1e` | 1.5 | console dew point (CH1) | 12-bit packed, temperature formula | decoded |
| `0x1f-0x29` | 10.5 | dew point, CH2-CH8 | `0x7fa` = no sensor | structure inferred |
| `0x2a` | 1.5 | console feels-like (CH1) | 12-bit packed, temperature formula | decoded |
| `0x2b-0x35` | 10.5 | feels-like, CH2-CH8 | `0x7fa` = no sensor | structure inferred |
| `0x36` | 2 LE | absolute pressure | `raw * 0.1` hPa | verified |
| `0x38` | 2 LE | relative pressure | `raw * 0.1` hPa | verified |
| `0x3a` | 1 | average wind speed | `raw * 0.1` m/s | verified |
| `0x3b` | 2 LE | wind gust | `raw * 0.00625` m/s | verified |
| `0x3d` | 2 LE | wind direction | degrees | verified |
| `0x3f` | 2 LE | rainfall, last hour | `raw * 0.1` mm | verified |
| `0x41` | 2 LE | rainfall, today | `raw * 0.1` mm | verified |
| `0x43` | 2 LE | rainfall, week | `raw * 0.1` mm | verified |
| `0x45` | 2 LE | rainfall, month | `(raw >> 4) * 0.1` mm | verified |
| `0x48` | 2 LE | rainfall, total | `raw * 0.1` mm | verified |
| `0x4a` | 1 | unknown | Always `0x00` in captures to date | unknown |
| `0x4b` | 1 | additive checksum | packet checksum | verified |

The high nibble of each temperature raw value is masked because the values are
**nibble-packed 12-bit fields**: consecutive values share a byte, so the upper
nibble belongs to the neighbouring slot. With no extra sensor present, a slot
reads `0x7fa`, which produces the repeating `7f fa a7` byte pattern.

### Multi-channel blocks and console-computed values (2026-09-24 capture)

The console supports thermometer/hygrometer channels CH1-CH8. A 24-hour capture
(`ft0203_unknown_24h.ndjson`, 5,382 packets, 2026-09-23 17:22 to 2026-09-24
17:22 UTC) shows four 8-slot blocks, one slot per channel:

| Block | Contents | Slot format |
|---|---|---|
| `0x0a-0x15` | temperature CH1-CH8 | 12-bit packed |
| `0x16-0x1d` | humidity CH1-CH8 | 1 byte, `0x7a` = no sensor |
| `0x1e-0x29` | dew point CH1-CH8 | 12-bit packed |
| `0x2a-0x35` | feels-like CH1-CH8 | 12-bit packed |

Only CH1 is populated. Over 24 hours the only changing unassigned bytes were
`0x1e`, `0x2a` and `0x2b`. Decoding `0x1e` and `0x2a` with the temperature
formula reproduces the reader's calculated dew point (mean error +0.03 C, max
0.15 C) and feels-like (mean +0.005 C, max 0.28 C) across every packet. So the
console transmits its own dew point and feels-like values. The small differences
come from the console's 0.1 F resolution. `0x2b` changes only as the high nibble
of the feels-like value; it is not a separate field.

### Sensor-presence byte `0x04`

Byte `0x04` was `0x3f` in every capture until 2026-09-22 10:34:36 UTC, and `0x7f`
afterwards. The change happened in the same packet in which rain data first
appeared (rain total 0.0 to 2.1 mm), when the rain gauge was paired. That makes
it a strong candidate for a sensor-presence or link bitmask: bit 6 (`0x40`) is
likely the rain gauge. The meaning of the remaining bits is unconfirmed. Test it
by removing a sensor's batteries and watching for the matching bit to clear.

## Unknown-Field Capture

For every current-reading packet, `ft0203_usb_read.py` records the
still-unexplained header bytes `0x02-0x06` individually in `unknown_bytes` and
as `unknown_ranges`, along with the unused CH2-CH8 slot blocks as
`channel_slots`. Decoded fields `station_dew_point`, `station_feels_like` and
`sensor_mask` are included in `fields`.

The tracker at `0x4a` is also retained as an unknown value. It was `0x00` for
the whole 24-hour capture; earlier values such as `0x4400`/`0x5b00` came from
reading `0x4a` as 16 bits, so its high byte was actually the checksum. The
record includes `unknown_changed_offsets` and the full `changed_offsets` list.
The live TUI shows the watched ranges, marks a range containing a change with
`*`, and highlights each changed byte in yellow.

## Derived Values

These are calculated by the reader rather than directly transmitted fields:

| Value | Inputs | Status |
|---|---|---|
| Dew point | outdoor temperature and humidity; Magnus formula | display-validated; matches console value at `0x1e` |
| Feels-like temperature | outdoor temperature, humidity, average wind; wind-chill/heat-index calculation | display-validated; matches console value at `0x2a` |

## Sensor Status, Battery, and RSSI

The console manual documents only binary status indicators:

- A received-data/search icon for the thermometer/hygrometer, anemometer, and
  rain gauge.
- A low-battery indicator for an external sensor.

It does not document battery voltage, battery percentage, RSSI, signal bars, or
link-quality values. A static scan of the archived WeatherHome application and
its USB/model DLLs found no battery, RSSI, signal-strength, link-quality, or
power-level feature strings. The verified current record has no assigned status
field for these values. Consequently, no sensor battery or RSSI entity should
be exposed until a packet bit is identified and verified against a console state.

The still-unknown temperature high-nibble metadata and unassigned packet bytes
are the most plausible places to investigate a binary status flag. They are not
evidence of a numeric signal or power measurement.

## Historical A1 Experiment

The WH1080-style address request is not valid for this USB protocol. The old
command produced this complete four-byte status/error-like packet:

```text
04 80 02 86
```

`0x04` is the packet length; `0x86` is the valid additive checksum. Bytes after
it in the 64-byte USB report are not part of the application packet.

## Evidence

- Live captures: `ft0203_usb_read_*.ndjson`
- Reader and tests: `ft0203_usb_read.py`, `test_ft0203_usb_read.py`
- Experiment log: `USB_FINDINGS.md`
- Manual: [Cotech FT0203 instruction manual](https://www.manualslib.com/manual/2013390/Cotech-Ft0203.html)
- WeatherHome archive and source analysis are described in `USB_FINDINGS.md`.
