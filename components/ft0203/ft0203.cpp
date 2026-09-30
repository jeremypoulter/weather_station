#include "ft0203.h"

#include <cinttypes>

#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#if defined(USE_ESP32_VARIANT_ESP32P4) || defined(USE_ESP32_VARIANT_ESP32S2) || defined(USE_ESP32_VARIANT_ESP32S3) || \
    defined(USE_ESP32_VARIANT_ESP32S31) || defined(USE_ESP32_VARIANT_ESP32H4)

namespace esphome {
namespace ft0203 {

static const char *const TAG = "ft0203";

static constexpr uint8_t INTERFACE_NUMBER = 0;
static constexpr uint8_t ENDPOINT_OUT = 0x04;
static constexpr uint8_t ENDPOINT_IN = 0x83;
static constexpr uint32_t EXCHANGE_TIMEOUT_MS = 4000;
static constexpr uint32_t RETRY_DELAY_MS = 2000;
static constexpr uint8_t UNAVAILABLE_AFTER_FAILURES = 3;

float field_value(const Reading &reading, Field field) {
  const auto index = static_cast<size_t>(field);
  const auto first_channel = static_cast<size_t>(Field::CH1_TEMPERATURE);
  if (index >= first_channel) {
    const Channel &channel = reading.channels[(index - first_channel) / 4];
    switch ((index - first_channel) % 4) {
      case 0:
        return channel.temperature;
      case 1:
        return channel.humidity;
      case 2:
        return channel.dew_point;
      default:
        return channel.feels_like;
    }
  }
  switch (field) {
    case Field::INDOOR_TEMPERATURE:
      return reading.indoor_temperature;
    case Field::INDOOR_HUMIDITY:
      return reading.indoor_humidity;
    case Field::ABSOLUTE_PRESSURE:
      return reading.absolute_pressure;
    case Field::RELATIVE_PRESSURE:
      return reading.relative_pressure;
    case Field::WIND_SPEED:
      return reading.wind_average;
    case Field::WIND_GUST:
      return reading.wind_gust;
    case Field::WIND_DIRECTION:
      return reading.wind_direction;
    case Field::RAIN_LAST_HOUR:
      return reading.rain_last_hour;
    case Field::RAIN_TODAY:
      return reading.rain_today;
    case Field::RAIN_WEEK:
      return reading.rain_week;
    case Field::RAIN_MONTH:
      return reading.rain_month;
    case Field::RAIN_TOTAL:
      return reading.rain_total;
    default:
      return NAN;
  }
}

void FT0203Hub::dump_config() {
  usb_host::USBClient::dump_config();
  ESP_LOGCONFIG(TAG, "  Update interval: %" PRIu32 " ms", this->interval_ms_);
  LOG_BINARY_SENSOR("  ", "Connected", this->connected_);
  for (size_t i = 0; i < FIELD_COUNT; i++) {
    LOG_SENSOR("  ", "Sensor", this->sensors_[i]);
  }
}

void FT0203Hub::on_connected() {
  this->session_.fetch_add(1);
  this->busy_.store(false);
  this->result_.store(Result::NONE);
  this->failures_ = 0;
  this->info_done_ = false;
  this->polled_ = false;
  this->not_before_ms_ = 0;

  auto err = usb_host_interface_claim(this->handle_, this->device_handle_, INTERFACE_NUMBER, 0);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Could not claim interface %u: %s", INTERFACE_NUMBER, esp_err_to_name(err));
    this->status_set_error(LOG_STR("Could not claim USB interface"));
    this->disconnect();
    return;
  }
  this->claimed_ = true;
  this->status_clear_error();
  ESP_LOGI(TAG, "FT-0203 connected");
  this->enable_loop();
}

void FT0203Hub::on_disconnected() {
  ESP_LOGI(TAG, "FT-0203 disconnected");
  this->session_.fetch_add(1);
  if (this->claimed_) {
    usb_host_endpoint_halt(this->device_handle_, ENDPOINT_IN);
    usb_host_endpoint_flush(this->device_handle_, ENDPOINT_IN);
    usb_host_endpoint_halt(this->device_handle_, ENDPOINT_OUT);
    usb_host_endpoint_flush(this->device_handle_, ENDPOINT_OUT);
    usb_host_interface_release(this->handle_, this->device_handle_, INTERFACE_NUMBER);
    this->claimed_ = false;
  }
  this->busy_.store(false);
  this->result_.store(Result::NONE);
  this->publish_unavailable_();
  this->set_responding_(false);
  usb_host::USBClient::on_disconnected();
}

void FT0203Hub::loop() {
  const bool had_work = this->process_usb_events_();
  if (this->state_ != usb_host::USB_CLIENT_CONNECTED || !this->claimed_) {
    if (!had_work)
      this->disable_loop();
    return;
  }

  const Result result = this->result_.exchange(Result::NONE, std::memory_order_acquire);
  if (result != Result::NONE) {
    this->handle_result_(result);
    return;
  }

  const uint32_t now = millis();
  if (this->busy_.load()) {
    if (now - this->exchange_started_ms_ > 2 * EXCHANGE_TIMEOUT_MS) {
      // The cancelled transfer never called back; give up on this exchange.
      ESP_LOGW(TAG, "Exchange %02x abandoned", this->command_);
      this->busy_.store(false);
      this->handle_result_(Result::FAILED);
    } else if (now - this->exchange_started_ms_ > EXCHANGE_TIMEOUT_MS && !this->recovering_) {
      // Transfers have no timeout of their own, so cancel the endpoints; the
      // cancelled transfer then reports failure through the normal path.
      ESP_LOGW(TAG, "No reply to command %02x, cancelling transfers", this->command_);
      this->recovering_ = true;
      this->recover_endpoints_();
    }
    return;
  }
  if (static_cast<int32_t>(now - this->not_before_ms_) < 0)
    return;

  if (!this->info_done_) {
    this->start_exchange_(COMMAND_INFO);
  } else if (!this->polled_ || now - this->last_poll_ms_ >= this->interval_ms_) {
    this->polled_ = true;
    this->last_poll_ms_ = now;
    this->start_exchange_(COMMAND_CURRENT);
  }
}

bool FT0203Hub::start_exchange_(uint8_t command) {
  this->busy_.store(true);
  this->recovering_ = false;
  this->command_ = command;
  this->exchange_started_ms_ = millis();
  this->assembler_.reset();

  // Only the two read-only requests are ever built here.
  uint8_t request[REPORT_SIZE] = {};
  request[0] = 3;
  request[1] = command;
  request[2] = static_cast<uint8_t>(3 + command);
  const uint32_t session = this->session_.load();
  const bool sent = this->transfer_out(
      ENDPOINT_OUT, [this, session](const usb_host::TransferStatus &status) { this->on_out_done_(session, status); },
      request, REPORT_SIZE);
  if (!sent) {
    this->busy_.store(false);
    this->handle_result_(Result::FAILED);
  }
  return sent;
}

// USB task context.
void FT0203Hub::on_out_done_(uint32_t session, const usb_host::TransferStatus &status) {
  if (session != this->session_.load())
    return;
  if (!status.success || !this->submit_read_(session))
    this->finish_exchange_(Result::FAILED);
}

// USB task context.
bool FT0203Hub::submit_read_(uint32_t session) {
  return this->transfer_in(
      ENDPOINT_IN, [this, session](const usb_host::TransferStatus &status) { this->on_in_done_(session, status); },
      REPORT_SIZE);
}

// USB task context.
void FT0203Hub::on_in_done_(uint32_t session, const usb_host::TransferStatus &status) {
  if (session != this->session_.load())
    return;
  if (!status.success) {
    this->finish_exchange_(Result::FAILED);
    return;
  }
  switch (this->assembler_.add(status.data, status.data_len)) {
    case PacketAssembler::Status::NEED_MORE:
      if (!this->submit_read_(session))
        this->finish_exchange_(Result::FAILED);
      break;
    case PacketAssembler::Status::COMPLETE:
      this->finish_exchange_(this->assembler_.data()[1] == this->command_ ? Result::PACKET : Result::FAILED);
      break;
    default:
      this->finish_exchange_(Result::FAILED);
      break;
  }
}

// USB task context: hand the result to the main loop.
void FT0203Hub::finish_exchange_(Result result) {
  this->result_.store(result, std::memory_order_release);
  this->enable_loop_soon_any_context();
  App.wake_loop_threadsafe();
}

void FT0203Hub::recover_endpoints_() {
  usb_host_endpoint_halt(this->device_handle_, ENDPOINT_IN);
  usb_host_endpoint_flush(this->device_handle_, ENDPOINT_IN);
  usb_host_endpoint_clear(this->device_handle_, ENDPOINT_IN);
  usb_host_endpoint_halt(this->device_handle_, ENDPOINT_OUT);
  usb_host_endpoint_flush(this->device_handle_, ENDPOINT_OUT);
  usb_host_endpoint_clear(this->device_handle_, ENDPOINT_OUT);
}

void FT0203Hub::handle_result_(Result result) {
  this->busy_.store(false);
  this->recovering_ = false;
  bool ok = false;
  if (result == Result::PACKET) {
    const uint8_t *packet = this->assembler_.data();
    const size_t length = this->assembler_.length();
    if (this->command_ == COMMAND_INFO) {
      ESP_LOGD(TAG, "Device info: %s", format_hex_pretty(packet, length).c_str());
      if (length > MODEL_BYTE_INDEX && packet[MODEL_BYTE_INDEX] != SUPPORTED_MODEL) {
        ESP_LOGW(TAG, "Unrecognised model byte 0x%02x (expected 0x%02x); readings may be wrong",
                 packet[MODEL_BYTE_INDEX], SUPPORTED_MODEL);
      }
      this->info_done_ = true;
      ok = true;
    } else {
      Reading reading;
      if (decode_current(packet, length, reading)) {
        ESP_LOGV(TAG, "Current record: %s", format_hex_pretty(packet, length).c_str());
        this->publish_reading_(reading);
        this->set_responding_(true);
        ok = true;
      } else {
        ESP_LOGW(TAG, "Unexpected current-record packet (%u bytes)", static_cast<unsigned>(length));
      }
    }
  }

  if (ok) {
    this->failures_ = 0;
    this->status_clear_warning();
    return;
  }
  this->failures_++;
  this->not_before_ms_ = millis() + RETRY_DELAY_MS;
  ESP_LOGW(TAG, "Exchange failed (%u in a row)", this->failures_);
  this->status_set_warning(LOG_STR("No valid reply from FT-0203"));
  if (this->failures_ == UNAVAILABLE_AFTER_FAILURES) {
    this->publish_unavailable_();
    this->set_responding_(false);
  }
  if (this->failures_ % UNAVAILABLE_AFTER_FAILURES == 0)
    this->recover_endpoints_();
}

void FT0203Hub::publish_reading_(const Reading &reading) {
  if (reading.sensor_mask != this->last_sensor_mask_) {
    ESP_LOGD(TAG, "Sensor mask 0x%02x, channel flags T 0x%02x H 0x%02x", reading.sensor_mask,
             reading.channel_temperature_flags, reading.channel_humidity_flags);
    this->last_sensor_mask_ = reading.sensor_mask;
  }
  for (size_t i = 0; i < FIELD_COUNT; i++) {
    if (this->sensors_[i] != nullptr)
      this->sensors_[i]->publish_state(field_value(reading, static_cast<Field>(i)));
  }
}

void FT0203Hub::publish_unavailable_() {
  for (auto *sensor : this->sensors_) {
    if (sensor != nullptr)
      sensor->publish_state(NAN);
  }
}

void FT0203Hub::set_responding_(bool responding) {
  if (this->responding_known_ && this->responding_ == responding)
    return;
  this->responding_known_ = true;
  this->responding_ = responding;
  if (this->connected_ != nullptr)
    this->connected_->publish_state(responding);
}

}  // namespace ft0203
}  // namespace esphome

#endif
