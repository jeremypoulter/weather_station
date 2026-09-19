# FT-0203 USB experiments — 2026-09-16

**Update, 2026-09-19:** the WeatherHome read protocol has now been verified on
the connected station. See **WeatherHome discovery and working read protocol**
below. The A1 experiments in the earlier sections are historical; the current
reader is `ft0203_usb_read.py`.

## Device and HID descriptor

Connected as `1130:0829`, TENX `TMU313X USB R/W64`, bus 003 device 024
at the time of testing. Interface 0 has interrupt IN `0x83` and OUT `0x04`,
both with a maximum packet size of 64 bytes and a 1 ms polling interval.
USB access worked without sudo.

The 39-byte HID report descriptor, obtained both from sysfs and GET_DESCRIPTOR:

```text
06 a0 ff 09 01 a1 01 09 01 15 00 26 ff 00 75 40
95 08 91 02 95 08 75 40 15 00 26 ff 00 09 01 19
00 2a ff 00 81 00 c0
```

This declares vendor-defined usage page `0xffa0`, one application collection,
and input/output reports each of 8 × 64 bits = 64 bytes. It declares no report
IDs or feature reports. The descriptor does not identify weather fields or
establish a memory-read command or address stride.

## Captures and reproduction

```bash
python3 ft0203_usb_poll_poc.py --start 0 --blocks 1 --capture --interval 2 --duration 90
python3 ft0203_usb_probe.py --duration 60
```

Local evidence files (ignored by the existing `*.ndjson` rule):

- `ft0203_capture_20260916_215439Z.ndjson`: 45 frames plus metadata.
- `ft0203_usb_probe_1789595821.ndjson`: 141 records, including the descriptor,
  exact requests, write results, responses, timeouts, and completion.

The probe uses the existing speculative WH1080-style command:
`A1 addrH addrL 20 A1 addrH addrL 20`, padded with zeros for 64-byte writes.
It tests addresses `0x0000`, `0x0020`, `0x0040`, `0x0100` in that order for
each transport, then polls address zero via interrupt OUT for 60 seconds.
It does not automatically fall back between transports. Experiments share
one device session; they are not independent power-on tests.

## Observations

| Experiment | Result |
|---|---|
| Original poller, 90 seconds, address zero | 45 identical 64-byte replies; each cycle took about 806 ms |
| Passive interrupt read before/after probe | No data within each 1-second timeout |
| HID GET_REPORT input, ID 0, length 64 | Returned 64 zero bytes |
| HID SET_REPORT output, ID 0, 8-byte command | All four writes accepted; no interrupt reply in subsequent reads |
| HID SET_REPORT output, ID 0, 64-byte padded command | All four control transfers timed out |
| Interrupt OUT, 64-byte padded command | All four addresses returned the same 64 bytes; read latency about 2 ms |
| Extra interrupt read after each address experiment | No additional data within 1 second |
| Interrupt-only time series, 60 seconds | 30 replies, all identical to the original capture |

Across both captures: **79 interrupt replies, one unique payload, zero changing
byte offsets**. First response: `21:54:39.982 UTC`; last: `21:58:20.870 UTC`.
There was a gap between the capture runs, so this was not continuous monitoring.
The GET_REPORT zero-filled response is separate from these 79 interrupt replies.

```text
offset  bytes
00      04 80 02 86 2e a5 07 b4 bf e6 f2 05 4d 9b 42 fb
10      a5 5d 8c f5 0a 66 01 4b b8 a6 c9 72 21 b5 e9 9d
20      be c2 e5 f5 22 7d 58 cb fe 6b ba 42 39 35 6a 7f
30      bc 2f 8f 96 0e b7 c9 bc 84 d2 50 31 a6 ff de 95
```

## Interpretation and next experiments

- Interrupt OUT followed by interrupt IN is a repeatable exchange. The original
  poller's roughly 800 ms delay is consistent with its control-path read timeout
  before interrupt fallback. Successful USB transfers do not validate the command.
- These samples provide no evidence for addressable memory or live weather
  values in this reply. Status, stale data, and an unrecognised command response
  remain possibilities. No display observations were collected, so we cannot
  assert that the displayed values changed during testing.
- Next, capture while deliberately observing a display value change (for example
  indoor temperature) and annotate its value and host timestamp. This tests
  whether the fixed reply responds to an actual measurement change.
- Obtain the station's companion software or a known working implementation.
  Capture application startup and a live refresh with USBPcap/Wireshark or a
  USB-passthrough VM. Identify the real initialisation and read exchanges before
  attempting field correlation.
- Keep the 32-byte versus 64-byte address stride unresolved until the application
  protocol is known; USB report size alone is insufficient evidence to change it.

## Follow-up: live display and companion software

The owner confirms that the connected station is receiving sensor data and its
display values change throughout the day. This is owner-observed context, not a
timestamped display/USB comparison from the above experiments. The companion
Windows software is called **Weather Home**; its original CD may be unavailable.
The USB investigation should not depend on getting Windows running.

An initial online search did not locate a verified Weather Home installer or
an FT-0203 USB implementation. It did locate related radio-protocol work:

- [rtl_433 issue #2569](https://github.com/merbanan/rtl_433/issues/2569):
  FT0203/18-3676 anemometer reverse engineering, including sample payloads,
  wind-speed scaling, direction bits, and CRC analysis.
- [rtl_433 decoder](https://github.com/merbanan/rtl_433/blob/b5f5eeb2a7dc22a8723c1a0a4b2d37ac4c91b90a/src/devices/cotech_ft0203.c):
  added in July 2026, disabled by default pending field verification. Describes
  a nine-byte radio payload, speeds in tenths of m/s and CRC-8 polynomial `0x31`.
  Some high wind-speed bits remain unconfirmed.

These sources concern the outdoor sensor's radio transmission, not the console's
64-byte USB protocol. Their field layouts must not be assumed to apply to USB.
They provide reference encodings if sensor packets are subsequently found inside
USB replies. The current invariant reply does not establish that connection.

Useful next steps without Windows: extend fixed-command observation across known
display changes; find a Weather Home installer for static examination of its
USB routines; use evidence from that software or device documentation to choose
initialisation/read commands. Merely accumulating more copies of the invariant
reply will not reveal a field mapping.

## Three-hour capture results

Analysed on 2026-09-19. Local capture:
`ft0203_usb_3h_20260918_165335Z.ndjson` (4,363,379 bytes).
Display reference: `ft0203_usb_3h_20260918_165335Z_observations.md`.

The run completed normally at **2026-09-18 19:53:57.603 UTC**.
The fixed-address interrupt-only series began at **16:53:56.208 UTC**;
its final response was at **19:53:54.600 UTC**.

| Metric (time series only) | Result |
|---|---|
| Requests / responses | 5,389 / 5,389 |
| Response size | 64 bytes for every response |
| Read/write errors or short writes | 0 |
| Unique payloads | 1 |
| Changing byte offsets | 0 |
| Consecutive payload changes | 0 |
| Minimum / maximum response interval | 2.003 / 2.057 seconds |
| Difference from the earlier 2026-09-16 capture payload | None |

The full file has no malformed JSON lines and contains a completion record.
Its error records belong to the explicit preliminary transport experiments and
passive reads, not to the three-hour polling series.

At the user observation recorded at **17:51:17 UTC**, the display showed outdoor
temperature **17.4**, outdoor humidity **65%**, indoor temperature **23.5**, and
indoor humidity **50%**. The nearest response was 0.345 seconds later and had the
same invariant payload. This timestamp proximity refers to recording the user's
message, not a hardware-synchronised observation.

### Conclusion

The current command produces a stable reply over three hours, identical even to
the earlier capture two days before. Together with the owner's report of changing
display values, this strongly argues against treating it as a live measurement
report. We still cannot identify it as an error, status, memory, or stale-buffer
reply. One labelled observation is insufficient to establish a field mapping.

Further fixed-command logging is unlikely to help. The next investigation should
identify the actual application command/initialisation sequence, ideally through
static inspection of Weather Home or documentation/known implementations for the
same console. Successful USB transport alone is not evidence of a valid weather
data request.

## WeatherHome discovery and working read protocol

### Software located

- [WeatherHome archive for Cotech 36-7959 / FT-0205](https://github.com/DjGeNeSiSxx/cotech-weatherhome-36-7959)
  says it contains the original CD software. This is a community archive, not
  a current Clas Ohlson download. The retailer's pages presented a client
  challenge during this search.
- [Pinned installer download](https://raw.githubusercontent.com/DjGeNeSiSxx/cotech-weatherhome-36-7959/3f1e3ca1031ceed1170e562aac0efcded9ceb208/WeatherHome.exe)
  (4,827,550 bytes), SHA-256:
  `4ca92367a11f83a7beceefcb432a1a18c31b386a3c5e98b1b52c796233391fd1`.
- Downloaded to `/tmp/opencode/WeatherHome-FT0205.exe`. Recognised as an Inno
  Setup 5.4.2 installer. Extracted selected files with `innoextract` into
  `/tmp/opencode/weatherhome-extracted/app/`; no Windows software was executed.
  The extraction tool was unpacked from its Ubuntu package locally, not installed
  system-wide.
- Contents include `WeatherHome.exe`, `WeatherHome.ocx`, `USBDriver.dll`,
  `WIFIDriver.dll`, and model modules `ID0040.dll`, `ID0080.dll`, `ID00C0.dll`.

`USBDriver.dll` SHA-256:
`84fc728dd605b0894a5a9221715cc3fceddbd4a171849f0e92ba77680935ed70`.
Static disassembly establishes:

| DLL address | Evidence |
|---|---|
| `0x100017f3`–`0x1000180e` | Checks VID `0x1130`, PID `0x0829`, device version `0x0100` |
| `0x10001a30` (`UsbReadDeviceInfo`) | Builds length 3, command `01` |
| `0x10001b50` (`UsbReadRecord`) | Builds length 3, command `04` |
| `0x10001360` (`exchange` helper) | Adds outgoing bytes modulo 256; uses 65-byte Windows HID buffer with leading zero report-ID placeholder |
| `0x1000147c`–`0x100014b8` | Reads a second report if length exceeds 64; validates incoming additive checksum, maximum length 128 |

Although the archive is labelled FT-0205, its USB transport matches our device.
Full application/model compatibility has not been tested.

### Source-driver comparison

The decisive source is
[SixthGenie/WeatherStationSniffer](https://github.com/SixthGenie/WeatherStationSniffer/tree/9ca1e387550184a79729e6f745e1b242220b5ca0),
which targets FT0203A and the exact VID/PID. Its Windows transport uses
`WriteFile`/`ReadFile`; its ESP32 transport explicitly strips/adds the Windows
report-ID placeholder around 64-byte raw USB transfers.

Other drivers examined in
[WeeWX commit 162959d8](https://github.com/weewx/weewx/tree/162959d8e69aab296e7ec47adf464d74ee334b86/src/weewx/drivers):

| Family / source | USB IDs | Wire protocol and relevance |
|---|---|---|
| Fine Offset WH1080, `fousb.py` | `1941:8021` | A1/address command via SET_REPORT, 32-byte memory reads. This is the source family of our unsuccessful guess, not this station's verified protocol. |
| Hideki TE923, `te923.py:1575–1639` | `1130:6801` | Same Tenx VID but different PID. `05 AF addrLo addrMid addrHi xor 00 00`; length-prefixed 8-byte reports, 32 data bytes plus header/checksum, explicit acknowledgement. Illustrates why sharing a USB-chip vendor does not establish command compatibility. |
| Oregon WMR100, `wmr100.py:271–299` | `0fde:ca01` | Initial control message `20 00 08 01 00 00 00 00`, then 8-byte input reports whose first byte gives useful byte count. Another example of report size differing from application data size. |
| FT0203A, `WeatherStationSniffer/WeatherStation.cpp:198–256` | `1130:0829` | Length + command + additive checksum, padded to 64 bytes; replies may span two reports. This exchange was successfully tested here. |

WeeWX is open-source. The FT0203A source repository has no standard root license
and includes usage restrictions in its text; the new Python reader implements
the observed wire protocol rather than importing its implementation.

### Verified exchange on our station

```text
Info request:     03 01 04       (zero-padded to 64 bytes, interrupt OUT 0x04)
Current request:  03 04 07       (zero-padded to 64 bytes, interrupt OUT 0x04)
Reply transport: interrupt IN 0x83, read 64-byte reports
Packet byte 0:   total application packet length, including checksum
Packet byte 1:   response command (01 or 04 on successful requests)
Last byte:       sum of preceding application bytes modulo 256
```

No leading report-ID zero is sent using PyUSB. It is a Windows HID API buffer
convention, not part of this device's raw USB report.

Test command:

```bash
python3 ft0203_usb_read.py --samples 5 --interval 5
```

Evidence: `ft0203_usb_read_1789836512.ndjson`, 2026-09-19 16:48:32–16:48:52 UTC.

- Info response: `0b010a4000fb738c400090` (11 bytes, checksum valid).
- Five current-reading replies: **76 bytes each**, reconstructed from two
  64-byte USB reports, all checksums valid and response code `04`.
- **Four distinct current packets**, with changing offsets `0x07`, `0x3a`,
  `0x3b`, `0x3c`, `0x3d`, `0x3e`, and checksum at `0x4b`.
- Zero exchange failures. No clock-setting, calibration, erase, or other
  configuration commands were sent.

First current packet, offsets including the length and command bytes:

```text
4c0401013f00007e043f34a47ffaa77ffaa77ffaa77f517a7a7a7a7a7a7af7a37ffaa77ffaa77ffaa77f34a47ffaa77ffaa77ffaa77f63275227047000e10000000000000000000000000038
```

### Correction to interpretation of the earlier fixed reply

The old reply starts `04 80 02 86`: length 4, response code `80`, payload/status
`02`, checksum `86` (`04 + 80 + 02`). The remaining 60 bytes are outside the
declared packet and should not have been analysed as measurement fields. They
are consistent with unused/stale report-buffer contents.

This is consistent with a negative/status response to the invalid A1 request;
the exact meaning of `02` remains unverified. The three-hour test repeatedly
captured this short response, not a 64-byte weather record.

### Field decoding: temperature/humidity validated

Using the FT0203A source's temperature/humidity formulas gives:

| Field | Offset / formula | Values in the new capture |
|---|---|---|
| Indoor temperature | `((LE16(0x07) & 0xfff) - 400) / 10` °F, then convert to °C | 23.89 → 23.83 °C |
| Outdoor temperature | Same formula at `0x0a` | 19.78 °C |
| Indoor humidity | Byte `0x09` | 63% |
| Outdoor humidity | Byte `0x16` | 81% |

The owner reported the display at **2026-09-19 17:05:47 UTC** as outdoor
**19.7 C / 82%** and indoor **23.9 C / 64%**. The immediately preceding USB
sample was collected 13–20 minutes earlier (16:48:32–16:48:52 UTC). Its indoor
temperature matches exactly at 23.89 C, outdoor temperature differs by 0.08 C
(consistent with display rounding and an intervening update), and humidity is
one percentage point lower in both channels (63% / 81%). This validates the
temperature formulas and the two humidity offsets for live values; it does not
constitute an exact time-synchronised humidity comparison.

Do not copy the whole reference decoder uncritically: its README puts pressure
at `0x34/0x36` whereas C++ uses `0x36/0x38`; gust scaling also differs, and its
wind-direction accessors disagree about one-byte versus two-byte width. Here
`LE16(0x36)/10 = 1008.3` hPa is plausible, whereas `LE16(0x34)/10 = 3267.9`
is not. Plausibility does not establish the field's meaning.

Next: resolve the pressure and wind fields using captured changes and the
extracted model DLLs, then implement a
decoder with packet fixtures and integrate the confirmed command into long-run
capture tooling. The bounded reader already rejects invalid lengths, truncated
packets, bad checksums and unexpected response codes. Offline framing checks
and a live five-sample run passed.
