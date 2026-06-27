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
- `ft0203_camera_ocr_poc.py`
  - Live camera OCR monitor for reading display values.
  - Supports interactive ROI selection and NDJSON logging for USB correlation.
- `ft0203_camera_profile.json`
  - Reusable OCR profile with FT-0203 field ROIs and tuning defaults.
  - Supports optional perspective warp corner calibration and fast OCR mode.
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

### Camera + USB Correlation Workflow

1. Put an NTP-synced clock app in view of the camera for the first few seconds.
2. Start camera recording first.
3. Start USB capture command.
4. Trigger a visible event (for example, press a button that changes display mode) and note approximate wall-clock time.
5. During capture, force known display changes (units toggle, pressure trend period, etc.) and hold each state for 20-30 seconds.
6. Stop capture with Ctrl+C.

This gives you stable timestamp anchors so you can align video-observed value changes with byte offsets in `ft0203_capture.ndjson`.

## Live Camera OCR Automation

This PoC can run live OCR against the station display and log recognized text with timestamps.

### Dependencies

Install Tesseract OCR engine and Python packages:

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr
$(which python3) -m pip install opencv-python pytesseract
```

### Run OCR Monitor

Auto-generate a timestamped OCR capture file:

```bash
cd /home/jpoulter/Dev/JeremyPoulter/weather_station
$(which python3) ./ft0203_camera_ocr_poc.py \
  --camera-index 0 \
  --capture \
  --interval 1.0 \
  --select-roi \
  --preview
```

Or use the prepared profile file (recommended):

```bash
cd /home/jpoulter/Dev/JeremyPoulter/weather_station
QT_QPA_PLATFORM=xcb $(which python3) ./ft0203_camera_ocr_poc.py \
  --profile ft0203_camera_profile.json \
  --capture \
  --only-changes \
  --preview
```

### Perspective Calibration (for moved camera)

When the camera position changes, calibrate panel corners once and reuse them in the profile.

1. Run corner picker and click the display corners in this order: top-left, top-right, bottom-right, bottom-left.

```bash
QT_QPA_PLATFORM=xcb $(which python3) ./ft0203_camera_ocr_poc.py \
  --profile ft0203_camera_profile.json \
  --select-corners \
  --preview \
  --max-samples 1
```

2. Copy the printed corner list into `display_corners` in `ft0203_camera_profile.json`.

With corners set, every frame is perspective-warped before OCR so ROI coordinates stay stable.

Useful options:

- `--roi x,y,w,h` set ROI without interactive selection.
- `--field-roi name:x,y,w,h` define per-field OCR boxes (repeat per field).
- `--profile ft0203_camera_profile.json` load camera and OCR defaults from profile.
- `--select-corners` interactively pick display corners for perspective warp.
- `--corners x1,y1,x2,y2,x3,y3,x4,y4` set corners directly from CLI.
- `--warp-size 640x480` set warped panel size.
- `--fast` use faster OCR path (fewer variants/PSM checks).
- `--variant-list ...` advanced override of allowed OCR variants.
- `--early-conf 80` stop evaluating a field once confidence threshold is reached.
- `--motion-threshold 1.5` skip OCR for fields whose ROI has barely changed.

### Profile Trainer

Use training mode to auto-suggest `field_overrides` for better per-field OCR.

```bash
QT_QPA_PLATFORM=xcb $(which python3) ./ft0203_camera_ocr_poc.py \
  --profile ft0203_camera_profile.json \
  --train-samples 60 \
  --train-out ft0203_profile_suggestions.json \
  --capture \
  --preview
```

Optional: train only selected fields:

```bash
QT_QPA_PLATFORM=xcb $(which python3) ./ft0203_camera_ocr_poc.py --profile ft0203_camera_profile.json --train-samples 60 --train-fields out_temp,pressure,in_temp,time --train-out ft0203_profile_suggestions.json --capture --preview
```

After training, inspect `ft0203_profile_suggestions.json`:

- `field_overrides` contains suggested `variant_list`, `psm_list`, `whitelist`, and `post_regex`.
- `training_summary` shows field-by-field quality (`valid_ratio`) so you can prioritize ROI fixes.

Auto-merge suggestions into your profile in one command:

```bash
QT_QPA_PLATFORM=xcb $(which python3) ./ft0203_camera_ocr_poc.py \
  --profile ft0203_camera_profile.json \
  --merge-suggestions ft0203_profile_suggestions.json \
  --capture
```

During training, auto-merge at the end:

```bash
QT_QPA_PLATFORM=xcb $(which python3) ./ft0203_camera_ocr_poc.py \
  --profile ft0203_camera_profile.json \
  --train-samples 80 \
  --train-out ft0203_profile_suggestions.json \
  --auto-merge-train \
  --capture \
  --preview
```

### Dual Live View

Use `--dual-preview` to show two windows simultaneously:

- Raw camera view (for physical alignment)
- Adjusted/warped OCR view with field labels

```bash
QT_QPA_PLATFORM=xcb $(which python3) ./ft0203_camera_ocr_poc.py --profile ft0203_camera_profile.json --capture --preview --dual-preview
```

In dual-preview mode, press `c` to reselect display corners live, and `q` to quit.
- `--only-changes` emit OCR records only when recognized text changes.
- `--duration 1800` stop after 30 minutes.
- `--max-samples 600` stop after fixed OCR sample count.
- `--dump-frame frame.jpg` save an initial frame to help pick ROI coordinates manually.
- `--psm-list 6,7,11 --scale 3.0 --min-conf 15` more robust OCR defaults for low-contrast LCDs.
- `--debug-candidates` include top OCR candidates in each output record.

Preview now shows:

- Highlighted ROI boxes for each field.
- Per-box labels in the form `name=value`.
- A compact side panel with the latest extracted field values.

If ROI selection/preview fails on Wayland with Qt plugin warnings, try:

```bash
QT_QPA_PLATFORM=xcb $(which python3) ./ft0203_camera_ocr_poc.py --camera-index 0 --capture --interval 1.0 --select-roi --preview
```

If GUI still fails, run headless by dumping one frame, choosing ROI coordinates manually, then rerun with `--roi` and without `--select-roi/--preview`:

```bash
$(which python3) ./ft0203_camera_ocr_poc.py --camera-index 0 --dump-frame frame.jpg --max-samples 1
$(which python3) ./ft0203_camera_ocr_poc.py --camera-index 0 --capture --interval 1.0 --roi 120,80,600,240 --only-changes
```

Each OCR NDJSON record includes:

- `type=ocr`
- `ts` and `ts_iso` timestamps
- `text` recognized display text
- `confidence` average token confidence (when available)
- `changed` marker versus previous OCR sample

### Recommended Combined Capture

Run USB capture and camera OCR together in two terminals so both streams are timestamped:

```bash
# terminal 1: USB
sudo -n $(which python3) ./ft0203_usb_poll_poc.py --start 0x0000 --blocks 8 --capture --interval 1.0

# terminal 2: camera OCR
$(which python3) ./ft0203_camera_ocr_poc.py --camera-index 0 --capture --interval 1.0 --select-roi --preview
```

Then correlate by nearest timestamp between USB `type=frame` records and camera `type=ocr` records.

### LCD OCR Tuning Notes

For this display, full-screen OCR is noisy. Better results come from multiple small field ROIs.

Example with named field boxes (adjust to your framing):

```bash
$(which python3) ./ft0203_camera_ocr_poc.py \
  --camera-index 4 \
  --capture \
  --interval 1.0 \
  --field-roi temp:166,132,120,95 \
  --field-roi hum:163,205,120,85 \
  --field-roi press:157,283,170,92 \
  --field-roi rain:385,205,95,80 \
  --psm-list 6,7,11 \
  --scale 3.0 \
  --min-conf 15 \
  --debug-candidates
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
