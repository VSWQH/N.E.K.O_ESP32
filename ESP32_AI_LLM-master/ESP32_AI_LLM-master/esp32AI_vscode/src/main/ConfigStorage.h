/* ConfigStorage.h - NVS配置读写封装 */
#ifndef CONFIG_STORAGE_H
#define CONFIG_STORAGE_H

#include <Arduino.h>
#include <Preferences.h>

#define DEFAULT_RELAY_PORT 8765

struct DeviceConfig {
    String wifiSsid;
    String wifiPassword;
    String dashscopeApiKey; // legacy only; relay mode does not use this on ESP32
    String voice;           // legacy only; relay mode keeps voice on server
    String instructions;    // legacy only; relay mode keeps persona on server
    String relayHost;
    String relayPath;
    uint16_t relayPort = DEFAULT_RELAY_PORT;
    uint8_t volume = 50;
    bool isConfigured = false;
};

class ConfigStorage {
public:
    ConfigStorage();
    DeviceConfig load();
    bool save(const DeviceConfig& config);
    bool isConfigured();
    bool clear();
    bool clearWiFi();

private:
    static const char* NVS_NAMESPACE;
};

#endif
