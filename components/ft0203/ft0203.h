#pragma once

#include <atomic>

#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/usb_host/usb_host.h"
#include "esphome/core/component.h"
#include "ft0203_protocol.h"

namespace esphome {
namespace ft0203 {

// Order matters: the four per-channel fields (temperature, humidity, dew point,
// feels like) are consecutive for CH1..CH8, and field_value() relies on it.
enum class Field : uint8_t {
  INDOOR_TEMPERATURE,
  INDOOR_HUMIDITY,
  ABSOLUTE_PRESSURE,
  RELATIVE_PRESSURE,
  WIND_SPEED,
  WIND_GUST,
  WIND_DIRECTION,
  RAIN_LAST_HOUR,
  RAIN_TODAY,
  RAIN_WEEK,
  RAIN_MONTH,
  RAIN_TOTAL,
  CH1_TEMPERATURE,
  CH1_HUMIDITY,
  CH1_DEW_POINT,
  CH1_FEELS_LIKE,
  CH2_TEMPERATURE,
  CH2_HUMIDITY,
  CH2_DEW_POINT,
  CH2_FEELS_LIKE,
  CH3_TEMPERATURE,
  CH3_HUMIDITY,
  CH3_DEW_POINT,
  CH3_FEELS_LIKE,
  CH4_TEMPERATURE,
  CH4_HUMIDITY,
  CH4_DEW_POINT,
  CH4_FEELS_LIKE,
  CH5_TEMPERATURE,
  CH5_HUMIDITY,
  CH5_DEW_POINT,
  CH5_FEELS_LIKE,
  CH6_TEMPERATURE,
  CH6_HUMIDITY,
  CH6_DEW_POINT,
  CH6_FEELS_LIKE,
  CH7_TEMPERATURE,
  CH7_HUMIDITY,
  CH7_DEW_POINT,
  CH7_FEELS_LIKE,
  CH8_TEMPERATURE,
  CH8_HUMIDITY,
  CH8_DEW_POINT,
  CH8_FEELS_LIKE,
  COUNT,
};

constexpr size_t FIELD_COUNT = static_cast<size_t>(Field::COUNT);

float field_value(const Reading &reading, Field field);

// Read-only USB host client for the Cotech FT-0203 console. It only ever sends
// the device-info (03 01 04) and current-record (03 04 07) requests.
class FT0203Hub : public usb_host::USBClient {
 public:
  FT0203Hub(uint16_t vid, uint16_t pid) : usb_host::USBClient(vid, pid) {}

  void loop() override;
  void dump_config() override;

  void set_update_interval(uint32_t interval_ms) { this->interval_ms_ = interval_ms; }
  void set_sensor(Field field, sensor::Sensor *sensor) { this->sensors_[static_cast<size_t>(field)] = sensor; }
  void set_connected_binary_sensor(binary_sensor::BinarySensor *sensor) { this->connected_ = sensor; }

 protected:
  enum class Result : uint8_t { NONE, PACKET, FAILED };

  void on_connected() override;
  void on_disconnected() override;

  bool start_exchange_(uint8_t command);
  void finish_exchange_(Result result);
  void on_out_done_(uint32_t session, const usb_host::TransferStatus &status);
  void on_in_done_(uint32_t session, const usb_host::TransferStatus &status);
  bool submit_read_(uint32_t session);
  void handle_result_(Result result);
  void recover_endpoints_();
  void publish_reading_(const Reading &reading);
  void publish_unavailable_();
  void set_responding_(bool responding);

  sensor::Sensor *sensors_[FIELD_COUNT]{};
  binary_sensor::BinarySensor *connected_{nullptr};
  PacketAssembler assembler_;
  uint32_t interval_ms_{16000};
  uint32_t last_poll_ms_{0};
  uint32_t exchange_started_ms_{0};
  uint32_t not_before_ms_{0};
  std::atomic<uint32_t> session_{0};
  std::atomic<bool> busy_{false};
  std::atomic<Result> result_{Result::NONE};
  uint8_t command_{0};
  uint8_t failures_{0};
  uint8_t last_sensor_mask_{0};
  bool claimed_{false};
  bool info_done_{false};
  bool polled_{false};
  bool recovering_{false};
  bool responding_{false};
  bool responding_known_{false};
};

}  // namespace ft0203
}  // namespace esphome
