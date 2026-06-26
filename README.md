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

1. Device access and endpoint discovery works.
2. Passive hidraw reading produced no frames unless the device is explicitly polled.
3. Active polling returns consistent 64-byte responses from the station.
4. Changing requested memory address did not change returned payload in current command mode.

This means transport is working, but field-level protocol decoding is still incomplete.

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
  --capture ft0203_capture.ndjson \
  --interval 1.0
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

### Camera + USB Correlation Workflow

1. Put an NTP-synced clock app in view of the camera for the first few seconds.
2. Start camera recording first.
3. Start USB capture command.
4. Trigger a visible event (for example, press a button that changes display mode) and note approximate wall-clock time.
5. During capture, force known display changes (units toggle, pressure trend period, etc.) and hold each state for 20-30 seconds.
6. Stop capture with Ctrl+C.

This gives you stable timestamp anchors so you can align video-observed value changes with byte offsets in `ft0203_capture.ndjson`.

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
