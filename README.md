# FT-0203 Weather Station

USB tooling for a Cotech FT-0203 weather station using the WeatherHome
protocol.

## Device

- USB VID:PID: `1130:0829`
- Product string: `TMU313X USB R/W64`
- Interface: HID
- Interrupt endpoints: IN `0x83`, OUT `0x04`
- USB report size: 64 bytes

## Current Status

The WeatherHome read protocol is verified against the attached station:

```text
Device information request: 03 01 04
Current reading request:    03 04 07
```

Each command is zero-padded to 64 bytes and sent through interrupt OUT `0x04`.
Replies arrive on interrupt IN `0x83`. Application packets are length-prefixed
and end with an additive checksum. Current readings are 76-byte packets that
span two USB reports.

Validated against the console display:

- Indoor temperature and humidity
- Outdoor temperature and humidity
- Absolute pressure at packet offset `0x36`
- Relative pressure at packet offset `0x38`
- Derived dew point and feels-like values
- Average wind at packet offset `0x3a`
- Wind gust: `LE16(0x3b) * 0.00625 m/s`
- Wind direction: `LE16(0x3d)` degrees
- Rain last hour, today, week, month, and total
- Console-computed dew point (`0x1e`) and feels-like (`0x2a`)

Absolute pressure at `0x36` and relative pressure at `0x38` are separate,
validated fields. Rain hour/day/week/total use direct tenths of millimetres;
the packed month value uses a four-bit right shift before the same scaling. Dew
point and feels-like are calculated from decoded readings and have been checked
against the console display.

The former WH1080-style A1 address command is not valid for this station. Its
invariant response is a four-byte status/error packet: `04 80 02 86`.

See [USB_PROTOCOL.md](USB_PROTOCOL.md) for the verified transport, framing,
and complete current-record map. [USB_FINDINGS.md](USB_FINDINGS.md) contains
the experiment log and WeatherHome source investigation.

## Live Reader

Run the interactive terminal dashboard:

```bash
python3 ft0203_usb_read.py
```

The reader requests a current packet every 16 seconds, displays decoded values
in place, writes raw reports and structured readings to a timestamped NDJSON
file, and runs until Ctrl+C.

Controls:

- `r`: show or hide the raw packet
- `h`: show help
- `q` or Ctrl+C: quit

Below the verified measurements, the dashboard shows the console's own dew
point and feels-like values, the candidate sensor-presence byte (`0x04`), the
still-unexplained header bytes `0x02-0x06`, and the unused CH2-CH8 channel
slots. A `*` marks a range containing a change, and each individual byte that
changed since the previous packet is highlighted in yellow. Measured values are
shown in green when they increased and red when they decreased since the
previous packet. The header bytes are recorded as `unknown_bytes` and
`unknown_ranges`, the channel slots as `channel_slots`, and changes as
`unknown_changed_offsets` in every `type=reading` NDJSON record. The dashboard
needs a terminal of at least 82 columns by 23 rows.

For line-oriented output:

```bash
python3 ft0203_usb_read.py --plain --samples 5 --interval 5
```

Useful options:

- `--duration 3600`: stop after one hour
- `--samples 20`: stop after 20 current packets
- `--interval 5`: request a current packet every five seconds
- `--out capture.ndjson`: choose a new NDJSON output path
- `--plain`: disable the dashboard; selected automatically when output is redirected

For a 24-hour interactive capture, run the reader in a terminal that remains
open:

```bash
python3 ft0203_usb_read.py --duration 86400
```

The default 16-second interval produces approximately 5,400 current-reading
packets in 24 hours. Use Ctrl+C to finish early; the NDJSON file is flushed
after every record.

Each successful current packet emits a `type=reading` NDJSON record containing
the original packet, changed offsets, raw field values, candidate decodes,
units, and a validation status for each field.

## Other Tools

- `ft0203_poc.py`: passive hidraw listener. The station does not emit
  unsolicited reports.
- `ft0203_usb_probe.py`: explicit historical transport experiments. It records
  control-transfer, interrupt, and report-descriptor results without automatic
  fallback.
- `ft0203_usb_poll_poc.py`: historical WH1080 A1 memory-read experiment. Keep
  it as protocol evidence; use `ft0203_usb_read.py` for current data.

## Tests

Run the captured-packet regression checks:

```bash
python3 -m unittest -v test_ft0203_usb_read.py
```

## USB Permissions

Direct USB access may require root privileges. To enable non-root access on
Linux, create a udev rule:

```bash
sudo tee /etc/udev/rules.d/99-ft0203.rules >/dev/null <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1130", ATTRS{idProduct}=="0829", MODE="0660", GROUP="plugdev", TAG+="uaccess"
SUBSYSTEM=="usb", ATTR{idVendor}=="1130", ATTR{idProduct}=="0829", MODE="0660", GROUP="plugdev", TAG+="uaccess"
EOF
sudo usermod -aG plugdev "$USER"
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Unplug and reconnect the station, then log out and back in if group membership
changed.
