/*
 * ESP32-S3 native-USB host smoke test for the FT-0203 (1130:0829).
 * Enumeration/descriptors only; no interface claim or weather-station commands.
 */
#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "usb/usb_host.h"

static const char *TAG = "ft0203_probe";
static usb_host_client_handle_t client;
static usb_device_handle_t device;
static uint8_t pending_address;
static bool pending_disconnect;

/* Called from usb_host_client_handle_events(), in the client task. */
static void client_event(const usb_host_client_event_msg_t *event, void *arg)
{
    (void)arg;
    switch (event->event) {
    case USB_HOST_CLIENT_EVENT_NEW_DEV:
        ESP_LOGI(TAG, "USB device arrived at address %u", event->new_dev.address);
        pending_address = event->new_dev.address;
        break;
    case USB_HOST_CLIENT_EVENT_DEV_GONE:
        ESP_LOGW(TAG, "USB device disconnected");
        if (device == event->dev_gone.dev_hdl) {
            pending_disconnect = true;
        }
        break;
    default:
        ESP_LOGI(TAG, "USB client event %d", event->event);
        break;
    }
}

static void describe_device(uint8_t address)
{
    esp_err_t err = usb_host_device_open(client, address, &device);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Open address %u failed: %s", address, esp_err_to_name(err));
        return;
    }

    const usb_device_desc_t *desc = NULL;
    err = usb_host_get_device_descriptor(device, &desc);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Read device descriptor failed: %s", esp_err_to_name(err));
        goto close;
    }
    ESP_LOGI(TAG, "VID:PID %04x:%04x, USB version %04x, class %02x, max packet %u",
             desc->idVendor, desc->idProduct, desc->bcdUSB,
             desc->bDeviceClass, desc->bMaxPacketSize0);
    if (desc->idVendor != 0x1130 || desc->idProduct != 0x0829) {
        ESP_LOGW(TAG, "Not the FT-0203; inspecting descriptors only");
    } else {
        ESP_LOGI(TAG, "FT-0203 identified successfully");
    }

    usb_device_info_t info;
    err = usb_host_device_info(device, &info);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Speed=%s, configuration=%u",
                 info.speed == USB_SPEED_FULL ? "full" :
                 info.speed == USB_SPEED_LOW ? "low" : "other",
                 info.bConfigurationValue);
        if (info.str_desc_product) {
            usb_print_string_descriptor(info.str_desc_product);
        }
    } else {
        ESP_LOGW(TAG, "Read device info failed: %s", esp_err_to_name(err));
    }

    const usb_config_desc_t *config = NULL;
    err = usb_host_get_active_config_descriptor(device, &config);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Active configuration / interfaces / endpoints:");
        usb_print_config_descriptor(config, NULL);
    } else {
        ESP_LOGW(TAG, "Read configuration failed: %s", esp_err_to_name(err));
    }

    ESP_LOGI(TAG, "Probe complete: no weather-station commands sent");
    return;

close:
    ESP_ERROR_CHECK(usb_host_device_close(client, device));
    device = NULL;
}

/* Host library event handling must run independently of client events. */
static void host_task(void *arg)
{
    (void)arg;
    for (;;) {
        uint32_t flags = 0;
        esp_err_t err = usb_host_lib_handle_events(portMAX_DELAY, &flags);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Host event error: %s", esp_err_to_name(err));
        }
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "ESP32-S3 USB host probe starting (UART logs 115200 baud)");
    ESP_LOGW(TAG, "Native USB port needs external VBUS (OTG Y cable)");

    const usb_host_config_t host_config = {
        .skip_phy_setup = false,
        .intr_flags = 0,
    };
    ESP_ERROR_CHECK(usb_host_install(&host_config));
    if (xTaskCreate(host_task, "usb_host_events", 4096, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "Could not create USB host event task");
        return;
    }

    const usb_host_client_config_t client_config = {
        .is_synchronous = false,
        .max_num_event_msg = 5,
        .async = {
            .client_event_callback = client_event,
            .callback_arg = NULL,
        },
    };
    ESP_ERROR_CHECK(usb_host_client_register(&client_config, &client));
    ESP_LOGI(TAG, "Host ready: waiting for a USB device on the native USB port");

    for (;;) {
        esp_err_t err = usb_host_client_handle_events(client, pdMS_TO_TICKS(1000));
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            ESP_LOGE(TAG, "Client event error: %s", esp_err_to_name(err));
        }
        if (pending_disconnect) {
            pending_disconnect = false;
            if (device != NULL) {
                ESP_ERROR_CHECK(usb_host_device_close(client, device));
                device = NULL;
            }
        }
        if (pending_address != 0) {
            uint8_t address = pending_address;
            pending_address = 0;
            if (device != NULL) {
                ESP_LOGW(TAG, "Already inspecting a device; ignoring address %u", address);
            } else {
                describe_device(address);
            }
        }
    }
}
