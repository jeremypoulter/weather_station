/*
 * ESP32-S3 native-USB host read test for the FT-0203 (1130:0829).
 *
 * Read-only: sends only the WeatherHome device-info (03 01 04) and
 * current-record (03 04 07) requests, then logs the replies as hex with a
 * small cross-check decode. No configuration, calibration, erase or clock
 * commands exist in this program.
 */
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "usb/usb_host.h"

#define STATION_VID          0x1130
#define STATION_PID          0x0829
#define STATION_INTERFACE    0
#define EP_OUT               0x04
#define EP_IN                0x83
#define REPORT_SIZE          64
#define MAX_PACKET           128
#define CMD_INFO             0x01
#define CMD_CURRENT          0x04
#define CURRENT_LENGTH       76
#define POLL_INTERVAL_MS     16000
#define WRITE_TIMEOUT_MS     1000
#define READ_TIMEOUT_MS      2000
#define MAX_FAILURES         3

static const char *TAG = "ft0203";
static usb_host_client_handle_t client;
static usb_device_handle_t device;
static uint8_t pending_address;
static volatile bool pending_disconnect;
static volatile bool station_active;
static SemaphoreHandle_t transfer_done;

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

static void transfer_callback(usb_transfer_t *transfer)
{
    xSemaphoreGive((SemaphoreHandle_t)transfer->context);
}

/* Submit and wait. On timeout the endpoint is halted and flushed so the
 * transfer completes (cancelled) and can be reused. */
static esp_err_t run_transfer(usb_transfer_t *transfer, uint32_t timeout_ms)
{
    xSemaphoreTake(transfer_done, 0);
    esp_err_t err = usb_host_transfer_submit(transfer);
    if (err != ESP_OK) {
        return err;
    }
    if (xSemaphoreTake(transfer_done, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
        uint8_t ep = transfer->bEndpointAddress;
        usb_host_endpoint_halt(transfer->device_handle, ep);
        usb_host_endpoint_flush(transfer->device_handle, ep);
        xSemaphoreTake(transfer_done, pdMS_TO_TICKS(1000));
        usb_host_endpoint_clear(transfer->device_handle, ep);
        return ESP_ERR_TIMEOUT;
    }
    return transfer->status == USB_TRANSFER_STATUS_COMPLETED ? ESP_OK : ESP_FAIL;
}

static void log_hex(const char *label, const uint8_t *data, size_t length)
{
    char text[MAX_PACKET * 2 + 1];
    for (size_t i = 0; i < length; i++) {
        snprintf(&text[i * 2], 3, "%02x", data[i]);
    }
    ESP_LOGI(TAG, "%s (%u bytes): %s", label, (unsigned)length, text);
}

/* Additive checksum over every byte but the last; the first byte is the length. */
static bool packet_valid(const uint8_t *packet, size_t length)
{
    uint8_t sum = 0;
    for (size_t i = 0; i + 1 < length; i++) {
        sum = (uint8_t)(sum + packet[i]);
    }
    return sum == packet[length - 1];
}

static esp_err_t exchange(usb_transfer_t *out, usb_transfer_t *in, uint8_t command,
                          uint8_t *packet, size_t *packet_length)
{
    memset(out->data_buffer, 0, REPORT_SIZE);
    out->data_buffer[0] = 3;
    out->data_buffer[1] = command;
    out->data_buffer[2] = (uint8_t)(3 + command);
    out->num_bytes = REPORT_SIZE;
    esp_err_t err = run_transfer(out, WRITE_TIMEOUT_MS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Write of command %02x failed: %s", command, esp_err_to_name(err));
        return err;
    }

    size_t received = 0;
    for (int report = 0; report < 3; report++) {
        in->num_bytes = REPORT_SIZE;
        err = run_transfer(in, READ_TIMEOUT_MS);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Read %d for command %02x failed: %s", report, command, esp_err_to_name(err));
            return err;
        }
        size_t count = in->actual_num_bytes;
        if (count == 0 || received + count > MAX_PACKET + REPORT_SIZE) {
            ESP_LOGE(TAG, "Unexpected report size %u", (unsigned)count);
            return ESP_ERR_INVALID_SIZE;
        }
        static uint8_t assembly[MAX_PACKET + REPORT_SIZE];
        memcpy(&assembly[received], in->data_buffer, count);
        received += count;
        uint8_t declared = assembly[0];
        if (declared < 3 || declared > MAX_PACKET) {
            ESP_LOGE(TAG, "Invalid declared length %u", declared);
            return ESP_ERR_INVALID_RESPONSE;
        }
        if (received >= declared) {
            if (!packet_valid(assembly, declared)) {
                ESP_LOGE(TAG, "Checksum mismatch");
                return ESP_ERR_INVALID_CRC;
            }
            if (assembly[1] != command) {
                ESP_LOGE(TAG, "Unexpected response code %02x", assembly[1]);
                return ESP_ERR_INVALID_RESPONSE;
            }
            memcpy(packet, assembly, declared);
            *packet_length = declared;
            return ESP_OK;
        }
    }
    return ESP_ERR_INVALID_RESPONSE;
}

static uint16_t le16(const uint8_t *packet, size_t offset)
{
    return (uint16_t)(packet[offset] | (packet[offset + 1] << 8));
}

/* Temperatures are 0.1 F with a +40 F offset; >= 0x7fa means no data. */
static void format_temperature(char *text, size_t size, uint16_t raw)
{
    if (raw >= 0x7fa) {
        snprintf(text, size, "--");
    } else {
        snprintf(text, size, "%.1f C", ((raw - 400) / 10.0 - 32.0) * 5.0 / 9.0);
    }
}

static void format_humidity(char *text, size_t size, uint8_t raw)
{
    if (raw >= 0x7a) {
        snprintf(text, size, "--");
    } else {
        snprintf(text, size, "%u %%", raw);
    }
}

/* Cross-check against the Python decoder: indoor and CH1 values, pressure. */
static void log_current(const uint8_t *packet)
{
    char indoor_t[16], ch1_t[16], indoor_h[16], ch1_h[16];
    format_temperature(indoor_t, sizeof(indoor_t), le16(packet, 0x07) & 0x0fff);
    format_humidity(indoor_h, sizeof(indoor_h), packet[0x09]);
    format_temperature(ch1_t, sizeof(ch1_t), le16(packet, 0x0a) & 0x0fff);
    format_humidity(ch1_h, sizeof(ch1_h), packet[0x16]);
    uint16_t relative = le16(packet, 0x38);
    ESP_LOGI(TAG, "indoor %s %s | ch1 %s %s | relative pressure %.1f hPa",
             indoor_t, indoor_h, ch1_t, ch1_h, relative >= 0x7ffa ? -1.0 : relative * 0.1);
}

static void station_task(void *arg)
{
    (void)arg;
    usb_transfer_t *out = NULL;
    usb_transfer_t *in = NULL;
    bool claimed = false;
    uint8_t packet[MAX_PACKET];
    size_t length = 0;
    int failures = 0;

    esp_err_t err = usb_host_interface_claim(client, device, STATION_INTERFACE, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Claim interface failed: %s", esp_err_to_name(err));
        goto finish;
    }
    claimed = true;
    if (usb_host_transfer_alloc(REPORT_SIZE, 0, &out) != ESP_OK ||
        usb_host_transfer_alloc(REPORT_SIZE, 0, &in) != ESP_OK) {
        ESP_LOGE(TAG, "Transfer allocation failed");
        goto finish;
    }
    out->device_handle = device;
    out->bEndpointAddress = EP_OUT;
    out->callback = transfer_callback;
    out->context = transfer_done;
    in->device_handle = device;
    in->bEndpointAddress = EP_IN;
    in->callback = transfer_callback;
    in->context = transfer_done;

    if (exchange(out, in, CMD_INFO, packet, &length) == ESP_OK) {
        log_hex("device info", packet, length);
    } else {
        failures++;
    }

    while (failures < MAX_FAILURES && !pending_disconnect) {
        if (exchange(out, in, CMD_CURRENT, packet, &length) == ESP_OK) {
            failures = 0;
            log_hex("current", packet, length);
            if (length == CURRENT_LENGTH) {
                log_current(packet);
            } else {
                ESP_LOGW(TAG, "Unexpected current-record length %u", (unsigned)length);
            }
        } else {
            failures++;
        }
        for (int waited = 0; waited < POLL_INTERVAL_MS && !pending_disconnect; waited += 100) {
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }

finish:
    if (in) {
        usb_host_transfer_free(in);
    }
    if (out) {
        usb_host_transfer_free(out);
    }
    if (claimed) {
        usb_host_interface_release(client, device, STATION_INTERFACE);
    }
    ESP_LOGI(TAG, "Station task finished");
    station_active = false;
    vTaskDelete(NULL);
}

static void open_device(uint8_t address)
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
        usb_host_device_close(client, device);
        device = NULL;
        return;
    }
    ESP_LOGI(TAG, "VID:PID %04x:%04x", desc->idVendor, desc->idProduct);
    if (desc->idVendor != STATION_VID || desc->idProduct != STATION_PID) {
        ESP_LOGW(TAG, "Not the FT-0203; ignoring");
        return;
    }
    ESP_LOGI(TAG, "FT-0203 identified; starting read-only exchange");
    station_active = true;
    if (xTaskCreate(station_task, "station", 6144, NULL, 4, NULL) != pdPASS) {
        ESP_LOGE(TAG, "Could not create station task");
        station_active = false;
    }
}

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
    ESP_LOGI(TAG, "ESP32-S3 FT-0203 read test starting (UART logs 115200 baud)");
    ESP_LOGW(TAG, "Native USB port needs external VBUS (OTG Y cable)");

    transfer_done = xSemaphoreCreateBinary();
    assert(transfer_done != NULL);

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
        esp_err_t err = usb_host_client_handle_events(client, pdMS_TO_TICKS(500));
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            ESP_LOGE(TAG, "Client event error: %s", esp_err_to_name(err));
        }
        /* Close only after the station task has released the interface. */
        if (pending_disconnect && !station_active && device != NULL) {
            usb_host_device_close(client, device);
            device = NULL;
            pending_disconnect = false;
        }
        if (pending_address != 0 && device == NULL) {
            uint8_t address = pending_address;
            pending_address = 0;
            open_device(address);
        }
    }
}
