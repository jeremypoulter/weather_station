# ESPHome component for the Cotech FT-0203

An ESPHome external component that reads the FT-0203 weather-station console
directly over USB from an ESP32-S3 and exposes the values to Home Assistant. No
computer, MQTT or collector service is involved.

It is **read-only**: it only sends the WeatherHome device-info (`03 01 04`) and
current-record (`03 04 07`) requests. It cannot change the station's settings,
calibration, history or clock.

## Hardware

- An ESP32-S3 board with a native USB port (tested on an ESP32-S3-DevKitC-1 v1.0).
- A USB host connection to the station that also supplies 5 V VBUS. The
  DevKitC-1 does not supply VBUS on its native port, so use an **OTG Y cable**
  with a USB power plug. See [../ESP32_S3_WIRING.md](../ESP32_S3_WIRING.md).
- Logs and flashing use the board's separate `UART` port.

The station has its own power supply and is only read over USB.

## Install

```yaml
external_components:
  - source:
      type: local          # or: github://jeremypoulter/weather_station
      path: ../components
    components: [ft0203]

logger:
  hardware_uart: UART0     # the native USB pins are the host, so not USB-Serial/JTAG

usb_host:

ft0203:
  update_interval: 16s     # minimum 5s

sensor:
  - platform: ft0203
    indoor_temperature:
      name: "Indoor temperature"
    ch1_temperature:
      name: "Outdoor temperature"

binary_sensor:
  - platform: ft0203
    connected:
      name: "Weather station USB"
```

[ft0203.yaml](ft0203.yaml) is a complete example. Copy
[secrets.yaml.example](secrets.yaml.example) to `secrets.yaml` and fill it in.

## Sensors

Every sensor is optional.

| Key | Unit | Notes |
|---|---|---|
| `indoor_temperature`, `indoor_humidity` | °C, % | console's built-in sensor |
| `ch1_temperature` ... `ch8_temperature` | °C | remote thermometer/hygrometer channels |
| `ch1_humidity` ... `ch8_humidity` | % | |
| `ch1_dew_point` ... `ch8_dew_point` | °C | computed by the console |
| `ch1_feels_like` ... `ch8_feels_like` | °C | computed by the console |
| `relative_pressure`, `absolute_pressure` | hPa | |
| `wind_speed`, `wind_gust` | m/s | |
| `wind_direction` | ° | |
| `rain_last_hour` | mm | rolling window |
| `rain_today`, `rain_week`, `rain_month`, `rain_total` | mm | reset at their boundary |

The `connected` binary sensor is on while the station is answering.

## Behaviour

- **No data:** when the station reports "no data" for a value, the sensor
  publishes an unknown state. This happens when a remote sensor stops
  transmitting or the anemometer battery is removed. The station keeps a sensor
  registered after it stops transmitting, so the component looks at the values,
  not the registration flags.
- **Rain:** rain values are unknown when the station does not report a rain gauge.
- **Failures:** after three failed exchanges in a row all sensors go unknown and
  `connected` turns off. The component keeps retrying.
- **Unplugging:** sensors go unknown and reconnect when the station is plugged
  back in.
- **One host only:** the station can talk to only one USB host. Unplug it from
  the computer before connecting it to the ESP32.

## Tests

The decoder, [ft0203_protocol.h](../components/ft0203/ft0203_protocol.h), has no
ESPHome dependencies. `python/test_esphome_decoder.py` compiles it natively and checks
it against the Python reference decoder on real captured packets:

```bash
python3 -m unittest discover -s python -v
```

Build the firmware with:

```bash
pip install esphome
cd esphome && esphome compile ft0203.yaml
```
