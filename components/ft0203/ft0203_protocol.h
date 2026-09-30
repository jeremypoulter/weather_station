#pragma once

// FT-0203 (WeatherHome "ID0040") protocol framing and current-record decoder.
//
// Deliberately free of ESPHome and ESP-IDF includes so it can be compiled and
// tested on a PC against the Python reference decoder (ft0203_usb_read.py).
// Field layout, widths and "no data" codes are documented in USB_PROTOCOL.md.

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace esphome {
namespace ft0203 {

constexpr uint8_t COMMAND_INFO = 0x01;
constexpr uint8_t COMMAND_CURRENT = 0x04;
constexpr size_t REPORT_SIZE = 64;
constexpr size_t MAX_PACKET_SIZE = 128;
constexpr size_t CURRENT_PACKET_LENGTH = 76;
constexpr size_t CHANNEL_COUNT = 8;
constexpr uint8_t MODEL_BYTE_INDEX = 3;
constexpr uint8_t SUPPORTED_MODEL = 0x40;

constexpr uint16_t INVALID_12BIT = 0x7FA;
constexpr uint8_t INVALID_HUMIDITY = 0x7A;
constexpr uint16_t INVALID_PRESSURE = 0x7FFA;

constexpr size_t OFFSET_CHANNEL_FLAGS_TEMPERATURE = 0x02;
constexpr size_t OFFSET_CHANNEL_FLAGS_HUMIDITY = 0x03;
constexpr size_t OFFSET_SENSOR_MASK = 0x04;
constexpr size_t OFFSET_INDOOR_TEMPERATURE = 0x07;
constexpr size_t OFFSET_INDOOR_HUMIDITY = 0x09;
constexpr size_t OFFSET_CHANNEL_TEMPERATURE = 0x0A;
constexpr size_t OFFSET_CHANNEL_HUMIDITY = 0x16;
constexpr size_t OFFSET_CHANNEL_DEW_POINT = 0x1E;
constexpr size_t OFFSET_CHANNEL_FEELS_LIKE = 0x2A;
constexpr size_t OFFSET_ABSOLUTE_PRESSURE = 0x36;
constexpr size_t OFFSET_RELATIVE_PRESSURE = 0x38;
constexpr size_t OFFSET_WIND_AVERAGE = 0x3A;
constexpr size_t OFFSET_WIND_GUST = 0x3B;
constexpr size_t OFFSET_WIND_DIRECTION = 0x3D;
constexpr size_t OFFSET_RAIN_LAST_HOUR = 0x3F;
constexpr size_t OFFSET_RAIN_TODAY = 0x41;
constexpr size_t OFFSET_RAIN_WEEK = 0x43;
constexpr size_t OFFSET_RAIN_MONTH = 0x45;
constexpr size_t OFFSET_RAIN_TOTAL = 0x48;
constexpr uint8_t SENSOR_MASK_RAIN = 0x40;

struct Channel {
  float temperature{NAN};  // degrees C
  float humidity{NAN};     // percent
  float dew_point{NAN};    // degrees C, computed by the console
  float feels_like{NAN};   // degrees C, computed by the console
};

// Every value is NAN when the station reports "no data" for it (for example a
// remote sensor that stopped transmitting, or the anemometer battery removed).
struct Reading {
  float indoor_temperature{NAN};
  float indoor_humidity{NAN};
  Channel channels[CHANNEL_COUNT];
  float absolute_pressure{NAN};  // hPa
  float relative_pressure{NAN};  // hPa
  float wind_average{NAN};       // m/s
  float wind_gust{NAN};          // m/s
  float wind_direction{NAN};     // degrees
  float rain_last_hour{NAN};     // mm
  float rain_today{NAN};
  float rain_week{NAN};
  float rain_month{NAN};
  float rain_total{NAN};
  uint8_t sensor_mask{0};
  uint8_t channel_temperature_flags{0};
  uint8_t channel_humidity_flags{0};
};

inline uint16_t le16(const uint8_t *packet, size_t offset) {
  return static_cast<uint16_t>(packet[offset] | (packet[offset + 1] << 8));
}

inline uint32_t le32(const uint8_t *packet, size_t offset) {
  return static_cast<uint32_t>(packet[offset]) | (static_cast<uint32_t>(packet[offset + 1]) << 8) |
         (static_cast<uint32_t>(packet[offset + 2]) << 16) | (static_cast<uint32_t>(packet[offset + 3]) << 24);
}

// Additive checksum over every byte but the last.
inline bool checksum_ok(const uint8_t *packet, size_t length) {
  if (length < 2)
    return false;
  uint8_t sum = 0;
  for (size_t i = 0; i + 1 < length; i++)
    sum = static_cast<uint8_t>(sum + packet[i]);
  return sum == packet[length - 1];
}

// Rebuilds one length-prefixed packet from the 64-byte USB reports it spans.
class PacketAssembler {
 public:
  enum class Status : uint8_t { NEED_MORE, COMPLETE, INVALID };

  void reset() { this->length_ = 0; }

  Status add(const uint8_t *data, size_t count) {
    if (count == 0 || this->length_ + count > sizeof(this->buffer_))
      return Status::INVALID;
    memcpy(this->buffer_ + this->length_, data, count);
    this->length_ += count;
    const uint8_t declared = this->buffer_[0];
    if (declared < 3 || declared > MAX_PACKET_SIZE)
      return Status::INVALID;
    if (this->length_ < declared)
      return Status::NEED_MORE;
    if (!checksum_ok(this->buffer_, declared))
      return Status::INVALID;
    this->length_ = declared;  // drop the report's zero padding
    return Status::COMPLETE;
  }

  const uint8_t *data() const { return this->buffer_; }
  size_t length() const { return this->length_; }

 private:
  uint8_t buffer_[MAX_PACKET_SIZE + REPORT_SIZE]{};
  size_t length_{0};
};

// Temperatures are 0.1 F units with a +40 F offset.
inline float temperature_celsius(uint16_t raw) {
  if (raw >= INVALID_12BIT)
    return NAN;
  return static_cast<float>(((raw - 400) / 10.0 - 32.0) * 5.0 / 9.0);
}

inline float humidity_percent(uint8_t raw) { return raw >= INVALID_HUMIDITY ? NAN : static_cast<float>(raw); }

inline float tenths(uint32_t raw) { return static_cast<float>(raw * 0.1); }

inline float tenths_valid(uint32_t raw, uint32_t invalid) { return raw >= invalid ? NAN : tenths(raw); }

// Slot `index` of a nibble-packed block of 12-bit values (two slots per three bytes).
inline uint16_t packed12_slot(const uint8_t *packet, size_t base, size_t index) {
  const uint16_t raw = le16(packet, base + index * 3 / 2);
  return (index % 2) ? static_cast<uint16_t>(raw >> 4) : static_cast<uint16_t>(raw & 0x0FFF);
}

// Decode a validated 76-byte current-record packet. Returns false if the packet
// is not a current record.
inline bool decode_current(const uint8_t *packet, size_t length, Reading &out) {
  if (length != CURRENT_PACKET_LENGTH || packet[0] != CURRENT_PACKET_LENGTH || packet[1] != COMMAND_CURRENT)
    return false;
  out = Reading{};
  out.sensor_mask = packet[OFFSET_SENSOR_MASK];
  out.channel_temperature_flags = packet[OFFSET_CHANNEL_FLAGS_TEMPERATURE];
  out.channel_humidity_flags = packet[OFFSET_CHANNEL_FLAGS_HUMIDITY];
  out.indoor_temperature = temperature_celsius(le16(packet, OFFSET_INDOOR_TEMPERATURE) & 0x0FFF);
  out.indoor_humidity = humidity_percent(packet[OFFSET_INDOOR_HUMIDITY]);
  for (size_t i = 0; i < CHANNEL_COUNT; i++) {
    Channel &channel = out.channels[i];
    channel.temperature = temperature_celsius(packed12_slot(packet, OFFSET_CHANNEL_TEMPERATURE, i));
    channel.humidity = humidity_percent(packet[OFFSET_CHANNEL_HUMIDITY + i]);
    channel.dew_point = temperature_celsius(packed12_slot(packet, OFFSET_CHANNEL_DEW_POINT, i));
    channel.feels_like = temperature_celsius(packed12_slot(packet, OFFSET_CHANNEL_FEELS_LIKE, i));
  }
  out.absolute_pressure = tenths_valid(le16(packet, OFFSET_ABSOLUTE_PRESSURE), INVALID_PRESSURE);
  out.relative_pressure = tenths_valid(le16(packet, OFFSET_RELATIVE_PRESSURE), INVALID_PRESSURE);
  // Wind values are 12-bit, nibble-packed from 0x3a.
  out.wind_average = tenths_valid(le16(packet, OFFSET_WIND_AVERAGE) & 0x0FFF, INVALID_12BIT);
  out.wind_gust = tenths_valid(le16(packet, OFFSET_WIND_GUST) >> 4, INVALID_12BIT);
  const uint16_t direction = le16(packet, OFFSET_WIND_DIRECTION) & 0x0FFF;
  out.wind_direction = direction >= INVALID_12BIT ? NAN : static_cast<float>(direction);
  // Rain counters are only valid when the rain-gauge bit of the sensor mask is set.
  if (packet[OFFSET_SENSOR_MASK] & SENSOR_MASK_RAIN) {
    out.rain_last_hour = tenths(le16(packet, OFFSET_RAIN_LAST_HOUR));
    out.rain_today = tenths(le16(packet, OFFSET_RAIN_TODAY));
    out.rain_week = tenths(le32(packet, OFFSET_RAIN_WEEK) & 0xFFFFF);
    out.rain_month = tenths((le32(packet, OFFSET_RAIN_MONTH) >> 4) & 0xFFFFF);
    out.rain_total = tenths(le32(packet, OFFSET_RAIN_TOTAL) & 0xFFFFF);
  }
  return true;
}

}  // namespace ft0203
}  // namespace esphome
