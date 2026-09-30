import esphome.codegen as cg
from esphome.components import sensor
import esphome.config_validation as cv
from esphome.const import (
    DEVICE_CLASS_ATMOSPHERIC_PRESSURE,
    DEVICE_CLASS_HUMIDITY,
    DEVICE_CLASS_PRECIPITATION,
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_WIND_DIRECTION,
    DEVICE_CLASS_WIND_SPEED,
    ICON_WEATHER_WINDY,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_MEASUREMENT_ANGLE,
    STATE_CLASS_TOTAL_INCREASING,
    UNIT_CELSIUS,
    UNIT_DEGREES,
    UNIT_HECTOPASCAL,
    UNIT_MILLIMETER,
    UNIT_PERCENT,
)

from . import CONF_FT0203_ID, FT0203Hub, ft0203_ns

DEPENDENCIES = ["ft0203"]

Field = ft0203_ns.enum("Field", is_class=True)

CHANNEL_COUNT = 8


def _temperature():
    return sensor.sensor_schema(
        unit_of_measurement=UNIT_CELSIUS,
        accuracy_decimals=1,
        device_class=DEVICE_CLASS_TEMPERATURE,
        state_class=STATE_CLASS_MEASUREMENT,
    )


def _humidity():
    return sensor.sensor_schema(
        unit_of_measurement=UNIT_PERCENT,
        accuracy_decimals=0,
        device_class=DEVICE_CLASS_HUMIDITY,
        state_class=STATE_CLASS_MEASUREMENT,
    )


def _pressure():
    return sensor.sensor_schema(
        unit_of_measurement=UNIT_HECTOPASCAL,
        accuracy_decimals=1,
        device_class=DEVICE_CLASS_ATMOSPHERIC_PRESSURE,
        state_class=STATE_CLASS_MEASUREMENT,
    )


def _wind_speed():
    return sensor.sensor_schema(
        unit_of_measurement="m/s",
        icon=ICON_WEATHER_WINDY,
        accuracy_decimals=1,
        device_class=DEVICE_CLASS_WIND_SPEED,
        state_class=STATE_CLASS_MEASUREMENT,
    )


def _rain(total: bool):
    # Today/week/month/total count up and reset at a boundary, which is what
    # total_increasing describes. "Last hour" is a rolling window and can fall.
    return sensor.sensor_schema(
        unit_of_measurement=UNIT_MILLIMETER,
        accuracy_decimals=1,
        device_class=DEVICE_CLASS_PRECIPITATION if total else cv.UNDEFINED,
        state_class=STATE_CLASS_TOTAL_INCREASING if total else STATE_CLASS_MEASUREMENT,
    )


# YAML key -> (schema, C++ Field name)
SENSORS = {
    "indoor_temperature": (_temperature(), "INDOOR_TEMPERATURE"),
    "indoor_humidity": (_humidity(), "INDOOR_HUMIDITY"),
    "absolute_pressure": (_pressure(), "ABSOLUTE_PRESSURE"),
    "relative_pressure": (_pressure(), "RELATIVE_PRESSURE"),
    "wind_speed": (_wind_speed(), "WIND_SPEED"),
    "wind_gust": (_wind_speed(), "WIND_GUST"),
    "wind_direction": (
        sensor.sensor_schema(
            unit_of_measurement=UNIT_DEGREES,
            accuracy_decimals=0,
            device_class=DEVICE_CLASS_WIND_DIRECTION,
            state_class=STATE_CLASS_MEASUREMENT_ANGLE,
        ),
        "WIND_DIRECTION",
    ),
    "rain_last_hour": (_rain(False), "RAIN_LAST_HOUR"),
    "rain_today": (_rain(True), "RAIN_TODAY"),
    "rain_week": (_rain(True), "RAIN_WEEK"),
    "rain_month": (_rain(True), "RAIN_MONTH"),
    "rain_total": (_rain(True), "RAIN_TOTAL"),
}

# The console has eight thermometer/hygrometer channels. Each reports its own
# temperature and humidity plus a dew point and feels-like computed by the console.
for _channel in range(1, CHANNEL_COUNT + 1):
    SENSORS[f"ch{_channel}_temperature"] = (_temperature(), f"CH{_channel}_TEMPERATURE")
    SENSORS[f"ch{_channel}_humidity"] = (_humidity(), f"CH{_channel}_HUMIDITY")
    SENSORS[f"ch{_channel}_dew_point"] = (_temperature(), f"CH{_channel}_DEW_POINT")
    SENSORS[f"ch{_channel}_feels_like"] = (_temperature(), f"CH{_channel}_FEELS_LIKE")

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_FT0203_ID): cv.use_id(FT0203Hub),
        **{cv.Optional(key): schema for key, (schema, _) in SENSORS.items()},
    }
)


async def to_code(config):
    hub = await cg.get_variable(config[CONF_FT0203_ID])
    for key, (_, field) in SENSORS.items():
        if key in config:
            sens = await sensor.new_sensor(config[key])
            cg.add(hub.set_sensor(getattr(Field, field), sens))
