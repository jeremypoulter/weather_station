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

### Seven-Segment OCR Model (important for accuracy)

The FT-0203 panel uses a seven-segment LCD font that the default Tesseract
`eng` model reads very poorly (it drops digits and decimal points). Accuracy
jumps from roughly 3% to ~94% by using a seven-segment trained model.

The `tessdata/` folder holds `ssd.traineddata` (from
[Shreeshrii/tessdata_ssd](https://github.com/Shreeshrii/tessdata_ssd)). The
profile enables it automatically via:

```json
"oem": 1,
"ocr_lang": "ssd",
"tessdata_dir": "tessdata"
```

You can also override per run:

```bash
--ocr-lang ssd --tessdata-dir tessdata --oem 1
```

To re-download the model:

```bash
mkdir -p tessdata
curl -fsSL -o tessdata/ssd.traineddata \
  https://github.com/Shreeshrii/tessdata_ssd/raw/master/ssd.traineddata
```

Seven-segment OCR commonly omits the decimal point and appends trailing glyphs
(seconds, the `WED` weekday, the `%` sign). The pipeline post-processes each
field with `format_field_value()` to re-insert the implied decimal place for
one-decimal fields and to rebuild `HH:MM` for the clock. Per-field ROIs are
tuned to exclude unit symbols (`°C`, `%`) and panel labels.

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

Live adjustment keys while preview is open:

- `w/a/s/d`: move OCR ROI
- `+` or `=`: grow ROI
- `-`: shrink ROI
- `r`: reselect ROI interactively
- `1/2/3/4`: select corner (TL/TR/BR/BL)
- `i/j/k/l`: nudge selected corner up/left/down/right
- `c`: re-pick all 4 corners
- `p`: save current ROI/corners/field boxes to profile
- `q`: quit

Mouse interactions in live preview:

- Drag the main OCR ROI box directly in the OCR preview window.
- Drag named field ROI boxes directly in the OCR preview window.
- In dual-preview mode, drag corner points directly in the raw view window.

Responsiveness note:

- Live display rendering and OCR processing run on separate threads.
- The UI keeps refreshing while OCR runs in the background.

Saving note:

- Use `p` during preview to persist the current geometry to `--profile`.
- Use `--save-profile-on-exit` to automatically save adjustments when the run ends.

Notes:

- Corner keys apply when perspective warp corners are enabled.
- You can use corner keys in both single preview and dual preview modes.
- `--only-changes` emit OCR records only when recognized text changes.
- `--duration 1800` stop after 30 minutes.
- `--max-samples 600` stop after fixed OCR sample count.
- `--dump-frame frame.jpg` save an initial frame to help pick ROI coordinates manually.
- `--psm-list 6,7,11 --scale 3.0 --min-conf 15` more robust OCR defaults for low-contrast LCDs.
- `--stabilize-window 7 --stabilize-min-votes 3` smooth noisy OCR by requiring repeat reads before updating values.
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
