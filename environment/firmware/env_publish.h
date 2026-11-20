// env01 上报 JSON 的唯一构造点。
//
// 手写 JSON 而不是用 ArduinoJson：报文只有固定几个数字和标识符，
// 手写便于在这里集中处理"缺测不发空值""设备没对时就不写采集时间"两条规则。
#pragma once

#include <cmath>
#include <cstdio>
#include <string>

namespace env {

inline std::string num3(float value) {
  char buf[24];
  snprintf(buf, sizeof(buf), "%.3f", (double) value);
  return std::string(buf);
}

// values/units 只放有限值；全 NaN 时返回空串，调用方不发送。
inline std::string values_object(bool is_sht, float temperature_c, float humidity_rh,
                                 const char *co2_key, float co2_ppm, std::string *units_out) {
  std::string values;
  std::string units;
  auto put = [&](const char *key, const char *unit, float value) {
    if (!std::isfinite(value)) return;
    if (!values.empty()) {
      values += ",";
      units += ",";
    }
    values += "\"";
    values += key;
    values += "\":";
    values += num3(value);
    units += "\"";
    units += key;
    units += "\":\"";
    units += unit;
    units += "\"";
  };
  if (is_sht) {
    put("temperature_c", "degC", temperature_c);
    put("humidity_rh", "%RH", humidity_rh);
  } else {
    put(co2_key, "ppm", co2_ppm);
    put("temperature_c", "degC", temperature_c);
    put("humidity_rh", "%RH", humidity_rh);
  }
  *units_out = units;
  return values;
}

inline std::string measurement(bool is_sht, const char *device_id, const char *sensor_id,
                               const char *zone_id, uint32_t boot_id, uint32_t seq,
                               uint32_t uptime_ms, bool clock_synced, long long epoch_ms,
                               const char *firmware_version, float temperature_c,
                               float humidity_rh, float co2_ppm) {
  std::string units;
  std::string values = values_object(is_sht, temperature_c, humidity_rh,
                                     "co2_ppm", co2_ppm, &units);
  if (values.empty()) return std::string();

  char head[512];
  char measured[32];
  char event_id[96];
  if (clock_synced) {
    snprintf(measured, sizeof(measured), "%lld", epoch_ms);
  } else {
    snprintf(measured, sizeof(measured), "null");
  }
  snprintf(event_id, sizeof(event_id), "%s-%u-%u", device_id, (unsigned) boot_id,
           (unsigned) seq);
  snprintf(head, sizeof(head),
           "{\"schema_version\":1,\"device_id\":\"%s\",\"sensor_id\":\"%s\","
           "\"zone_id\":\"%s\",\"boot_id\":%u,\"seq\":%u,\"event_id\":\"%s\","
           "\"device_uptime_ms\":%u,\"measured_at_ms\":%s,\"clock_status\":\"%s\"",
           device_id, sensor_id, zone_id, (unsigned) boot_id, (unsigned) seq, event_id,
           (unsigned) uptime_ms, measured, clock_synced ? "synced" : "unsynced");

  std::string out(head);
  out += ",\"values\":{";
  out += values;
  out += "},\"units\":{";
  out += units;
  out += "},\"quality\":\"ok\",\"error_code\":null,\"firmware_version\":\"";
  out += firmware_version;
  out += "\"}";
  return out;
}

inline std::string status(const char *device_id, const char *zone_id, uint32_t boot_id,
                          uint32_t seq, uint32_t uptime_ms, bool clock_synced,
                          float wifi_rssi_dbm, const char *firmware_version) {
  char rssi_buf[16];
  if (std::isfinite(wifi_rssi_dbm)) {
    snprintf(rssi_buf, sizeof(rssi_buf), "%d", (int) lroundf(wifi_rssi_dbm));
  } else {
    snprintf(rssi_buf, sizeof(rssi_buf), "null");
  }
  char head[512];
  char event_id[96];
  snprintf(event_id, sizeof(event_id), "%s-%u-%u", device_id, (unsigned) boot_id,
           (unsigned) seq);
  snprintf(head, sizeof(head),
           "{\"schema_version\":1,\"device_id\":\"%s\",\"zone_id\":\"%s\","
           "\"boot_id\":%u,\"seq\":%u,\"event_id\":\"%s\",\"uptime_ms\":%u,"
           "\"clock_status\":\"%s\","
           "\"wifi_rssi_dbm\":%s,\"ip\":null,\"firmware_version\":\"%s\"}",
           device_id, zone_id, (unsigned) boot_id, (unsigned) seq, event_id,
           (unsigned) uptime_ms, clock_synced ? "synced" : "unsynced", rssi_buf,
           firmware_version);
  return std::string(head);
}

}  // namespace env
