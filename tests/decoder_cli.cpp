// Reads hex-encoded FT-0203 packets on stdin (one per line) and prints the
// decoded fields as `name=value` lines, with a `--` line between packets.
// Used by test_esphome_decoder.py to compare against the Python reference.
#include <cmath>
#include <cstdio>
#include <iostream>
#include <string>
#include <vector>

#include "../components/ft0203/ft0203_protocol.h"

using namespace esphome::ft0203;

static void print_value(const char *name, float value) {
  if (std::isnan(value)) {
    std::printf("%s=nan\n", name);
  } else {
    std::printf("%s=%.6f\n", name, static_cast<double>(value));
  }
}

int main() {
  std::string line;
  while (std::getline(std::cin, line)) {
    std::vector<uint8_t> bytes;
    for (size_t i = 0; i + 1 < line.size(); i += 2)
      bytes.push_back(static_cast<uint8_t>(std::stoi(line.substr(i, 2), nullptr, 16)));

    // Feed the packet through the assembler as two 64-byte reports, like the device does.
    PacketAssembler assembler;
    PacketAssembler::Status status = PacketAssembler::Status::NEED_MORE;
    for (size_t offset = 0; offset < bytes.size() && status == PacketAssembler::Status::NEED_MORE;
         offset += REPORT_SIZE) {
      uint8_t report[REPORT_SIZE] = {};
      size_t count = std::min(REPORT_SIZE, bytes.size() - offset);
      std::memcpy(report, bytes.data() + offset, count);
      status = assembler.add(report, REPORT_SIZE);
    }
    Reading reading;
    if (status != PacketAssembler::Status::COMPLETE ||
        !decode_current(assembler.data(), assembler.length(), reading)) {
      std::printf("error=invalid\n--\n");
      continue;
    }
    print_value("indoor_temperature", reading.indoor_temperature);
    print_value("indoor_humidity", reading.indoor_humidity);
    for (size_t i = 0; i < CHANNEL_COUNT; i++) {
      char name[32];
      std::snprintf(name, sizeof(name), "ch%zu_temperature", i + 1);
      print_value(name, reading.channels[i].temperature);
      std::snprintf(name, sizeof(name), "ch%zu_humidity", i + 1);
      print_value(name, reading.channels[i].humidity);
      std::snprintf(name, sizeof(name), "ch%zu_dew_point", i + 1);
      print_value(name, reading.channels[i].dew_point);
      std::snprintf(name, sizeof(name), "ch%zu_feels_like", i + 1);
      print_value(name, reading.channels[i].feels_like);
    }
    print_value("absolute_pressure", reading.absolute_pressure);
    print_value("relative_pressure", reading.relative_pressure);
    print_value("wind_average", reading.wind_average);
    print_value("wind_gust", reading.wind_gust);
    print_value("wind_direction", reading.wind_direction);
    print_value("rain_last_hour", reading.rain_last_hour);
    print_value("rain_today", reading.rain_today);
    print_value("rain_week", reading.rain_week);
    print_value("rain_month", reading.rain_month);
    print_value("rain_total", reading.rain_total);
    std::printf("--\n");
  }
  return 0;
}
