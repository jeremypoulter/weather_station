# FT-0203 Weather Station PoC

This directory contains proof-of-concept tooling for a Cotexh FT-0203 weather station that enumerates as:

- USB VID:PID `1130:0829`
- Product string: `TMU313X USB R/W64`
- Interface class: HID (`usbhid`)
- Interrupt endpoints: IN `0x83`, OUT `0x04`
- Interrupt packet size: `64` bytes

## Files

- `ft0203_poc.py`
  - Passive HID raw listener (reads from `/dev/hidrawX`).
  - Useful for checking whether unsolicited reports appear.
- `ft0203_usb_poll_poc.py`
  - Active USB poller PoC.
  - Sends WH1080-style read command patterns and reads response frames.
  - Includes long-running capture mode with NDJSON timestamped output.
- `ft0203_frames.ndjson`
  - Captured frame file from passive listener runs.

## Current Findings

As of 2026-09-19, **the WeatherHome read protocol works on this station**:

1. Send `03 01 04` (device info) or `03 04 07` (current readings), zero-padded
   to 64 bytes, via interrupt OUT `0x04`.
2. Read reports from interrupt IN `0x83`. Byte zero gives the application packet
   length; the final application byte is an additive checksum.
3. Current readings are 76-byte packets spanning two USB reports. Five test
   readings produced four distinct packets, all with valid checksums.
4. The earlier WH1080-style A1 command produced an invariant four-byte
   status/error-like packet (`04 80 02 86`), followed by unused report bytes.
   The older poller and probe scripts preserve that historical experiment.

Use the new bounded reader:

```bash
python3 ft0203_usb_read.py
```

This opens an in-place terminal dashboard, records raw reports and validated
packets to an auto-named NDJSON file, and runs until Ctrl+C. The default
interval is 16 seconds. `r` toggles the raw packet and `h` shows a short help
message. Use `q` or Ctrl+C to quit.

For a log-friendly one-line reading per packet:

```bash
python3 ft0203_usb_read.py --plain --samples 5 --interval 5
```

Useful options:

- `--duration 3600` stops after one hour.
- `--samples 20` stops after 20 current-reading packets.
- `--out file.ndjson` chooses a new NDJSON destination.
- `--plain` disables the interactive dashboard; it is selected automatically
  when output is redirected.

Each valid current packet emits a structured `type=reading` NDJSON record with
the original packet, byte changes, raw values, and decoded fields. Indoor and
outdoor temperature/humidity are display-validated. Pressure, wind, direction,
and rain candidates are clearly marked provisional or conflicting in both
output modes.
See [USB_FINDINGS.md](USB_FINDINGS.md) for captures, the WeatherHome installer
source, driver comparisons, and the verified protocol.

## Run

Use the same Python used during PoC setup (has `pyusb` installed):

```bash
cd /home/jpoulter/Dev/JeremyPoulter/weather_station
sudo -n $(which python3) ./ft0203_usb_poll_poc.py --start 0x0000 --blocks 8
```

Try multiple addresses:

```bash
sudo -n $(which python3) ./ft0203_usb_poll_poc.py --start 0x0000 --blocks 8
sudo -n $(which python3) ./ft0203_usb_poll_poc.py --start 0x0100 --blocks 8
sudo -n $(which python3) ./ft0203_usb_poll_poc.py --start 0x1000 --blocks 8
```

### Long-Running Capture (for Video Correlation)

Use capture mode to log timestamped USB frames continuously while filming the station display.

```bash
cd /home/jpoulter/Dev/JeremyPoulter/weather_station
sudo -n $(which python3) ./ft0203_usb_poll_poc.py \
  --start 0x0000 \
  --blocks 8 \
  --capture \
  --interval 1.0
```

When `--capture` is provided without a filename, the script auto-creates a UTC timestamped file such as `ft0203_capture_20260626_223501Z.ndjson`.

You can still provide an explicit file path:

```bash
sudo -n $(which python3) ./ft0203_usb_poll_poc.py --start 0x0000 --blocks 8 --capture ft0203_capture.ndjson --interval 1.0
```

Useful options:

- `--duration 1800` stop automatically after 30 minutes.
- `--max-cycles 600` stop after fixed number of polls.
- `--only-changes` only write records when payload changes for a given block.

NDJSON output includes:

- `type=meta` session metadata at start.
- `type=frame` entries with Unix timestamp (`ts`), ISO UTC timestamp (`ts_iso`), poll `cycle`, block `address`, and payload hex (`hex`).

Example:

```json
{"type":"frame","ts":1761400000.123,"ts_iso":"2025-10-25T09:46:40.123000+00:00","cycle":12,"address":0,"index":0,"len":64,"hex":"...","changed":true}
```

## Notes on Permissions

By default, hidraw and direct USB access may require root privileges.

To enable non-root access on Linux, create a udev rule for this device.

1. Create the rule file:

```bash
sudo tee /etc/udev/rules.d/99-ft0203.rules >/dev/null <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1130", ATTRS{idProduct}=="0829", MODE="0660", GROUP="plugdev", TAG+="uaccess"
SUBSYSTEM=="usb", ATTR{idVendor}=="1130", ATTR{idProduct}=="0829", MODE="0660", GROUP="plugdev", TAG+="uaccess"
EOF
```

2. Ensure your user is in `plugdev`:

```bash
sudo usermod -aG plugdev $USER
```

3. Reload rules and trigger:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

4. Unplug/replug the weather station, then verify:

```bash
ls -l /dev/hidraw*
```

After this, you should be able to run the poller without `sudo`:

```bash
cd /home/jpoulter/Dev/JeremyPoulter/weather_station
$(which python3) ./ft0203_usb_poll_poc.py --start 0x0000 --blocks 8
```

If group membership was changed, log out and back in once before retesting.

## Suggested Next Steps

1. Collect a dataset while known values on the station display change.
2. Correlate byte offsets against known temperature/humidity/pressure/wind/rain values.
3. Build a first decoder module that emits candidate parsed fields with confidence scores.
