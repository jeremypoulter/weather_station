# ESP32-S3 USB host enumeration probe

This isolated PlatformIO + ESP-IDF project uses the **native ESP32-S3 USB port**
as a full-speed USB host. It prints USB device/configuration descriptors to the
separate **UART** serial port. It does **not** claim an interface, open an
endpoint, or send any FT-0203 application commands.

## Important before connecting the weather station

The DevKitC-1 v1.0 native `USB` port does not supply host VBUS (diode D7 in
the schematic blocks it), so a plain OTG adapter reads 0 V and nothing
enumerates. An **OTG Y cable** with a separate 5 V USB power plug fixes this.
Do not inject 5 V from a GPIO or splice power into the cable. The ESP32-S3 host
mode is selected by firmware, not by the cable's ID pin.

**Result (2026-09-30):** with the Y cable the probe enumerated the FT-0203:
`1130:0829`, full speed, product `TMU313X USB R/W64`, one HID interface with
interrupt IN `0x83` and OUT `0x04`, 64-byte packets, `bMaxPower` 100 mA. This
matches the Linux descriptors.

## Build and test the board alone

From the repository root:

```sh
pio run -d esp32_usb_probe
```

Power the board from the **UART** Micro-USB connector (`/dev/ttyUSB3`). Before
flashing, **unplug anything from the other (native `USB`) connector**. Then:

```sh
pio run -d esp32_usb_probe -t upload
pio device monitor -p /dev/ttyUSB3 -b 115200
```

Expected startup message:

```text
Host ready: waiting for a USB device on the native USB port
```

On enumeration the probe prints VID:PID, speed and the configuration
descriptors, then stops without opening an endpoint or sending any command.

The generic PlatformIO board profile is 8 MB flash, and the board's boot log
reports an **8 MB flash chip**. This small test is configured for **2 MB** as
a conservative image/partition size; the compiled binary and partition table
fit within 2 MB. If upload identifies a smaller device or a different board,
stop and confirm its module marking.
