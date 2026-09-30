import esphome.codegen as cg
from esphome.components import usb_host
import esphome.config_validation as cv
from esphome.const import CONF_UPDATE_INTERVAL

CODEOWNERS = ["@jeremypoulter"]
AUTO_LOAD = ["usb_host", "sensor", "binary_sensor"]
DEPENDENCIES = ["usb_host"]

CONF_FT0203_ID = "ft0203_id"

USB_VID = 0x1130
USB_PID = 0x0829

ft0203_ns = cg.esphome_ns.namespace("ft0203")
FT0203Hub = ft0203_ns.class_("FT0203Hub", usb_host.USBClient)

CONFIG_SCHEMA = usb_host.usb_device_schema(
    cls=FT0203Hub, vid=USB_VID, pid=USB_PID
).extend(
    {
        cv.Optional(CONF_UPDATE_INTERVAL, default="16s"): cv.All(
            cv.positive_time_period_milliseconds,
            cv.Range(min=cv.TimePeriod(seconds=5)),
        ),
    }
)


async def to_code(config):
    var = await usb_host.register_usb_client(config)
    cg.add(var.set_update_interval(config[CONF_UPDATE_INTERVAL]))
