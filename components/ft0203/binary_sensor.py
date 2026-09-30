import esphome.codegen as cg
from esphome.components import binary_sensor
import esphome.config_validation as cv
from esphome.const import DEVICE_CLASS_CONNECTIVITY

from . import CONF_FT0203_ID, FT0203Hub

DEPENDENCIES = ["ft0203"]

CONF_CONNECTED = "connected"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_FT0203_ID): cv.use_id(FT0203Hub),
        cv.Required(CONF_CONNECTED): binary_sensor.binary_sensor_schema(
            device_class=DEVICE_CLASS_CONNECTIVITY,
        ),
    }
)


async def to_code(config):
    hub = await cg.get_variable(config[CONF_FT0203_ID])
    sens = await binary_sensor.new_binary_sensor(config[CONF_CONNECTED])
    cg.add(hub.set_connected_binary_sensor(sens))
