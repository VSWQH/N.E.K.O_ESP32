/* ConfigStorage.cpp - NVS配置读写 */
#include "ConfigStorage.h"

const char* ConfigStorage::NVS_NAMESPACE = "voice_config";

ConfigStorage::ConfigStorage() {}

DeviceConfig ConfigStorage::load() {
    DeviceConfig config;
    config.wifiSsid = "";
    config.wifiPassword = "";
    config.dashscopeApiKey = "";
    config.voice = "";
    config.instructions = "";
    config.relayHost = "";
    config.relayPath = "/";
    config.relayPort = DEFAULT_RELAY_PORT;
    config.volume = 50;
    config.isConfigured = false;

    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, true)) return config;

    config.wifiSsid = prefs.getString("wifi_ssid", "");
    config.wifiPassword = prefs.getString("wifi_pwd", "");
    config.dashscopeApiKey = prefs.getString("ds_api_key", "");
    config.voice = prefs.getString("voice", "");
    config.instructions = prefs.getString("instr", "");
    config.relayHost = prefs.getString("relay_host", "");
    config.relayPath = prefs.getString("relay_path", "/");
    if (config.relayPath.length() == 0) config.relayPath = "/";
    config.relayPort = prefs.getUShort("relay_port", DEFAULT_RELAY_PORT);
    config.volume = prefs.getUChar("volume", 50);
    config.isConfigured = prefs.getBool("configured", false);

    prefs.end();
    return config;
}

bool ConfigStorage::save(const DeviceConfig& config) {
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, false)) return false;

    prefs.putString("wifi_ssid", config.wifiSsid);
    prefs.putString("wifi_pwd", config.wifiPassword);
    prefs.putString("ds_api_key", "");
    prefs.putString("voice", "");
    prefs.putString("instr", "");
    prefs.putString("relay_host", config.relayHost);
    prefs.putString("relay_path", config.relayPath.length() ? config.relayPath : "/");
    prefs.putUShort("relay_port", config.relayPort);
    prefs.putUChar("volume", config.volume);
    prefs.putBool("configured", true);

    prefs.end();
    return true;
}

bool ConfigStorage::isConfigured() {
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, true)) return false;
    bool result = prefs.getBool("configured", false);
    prefs.end();
    return result;
}

bool ConfigStorage::clear() {
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, false)) return false;
    prefs.clear();
    prefs.end();
    return true;
}

bool ConfigStorage::clearWiFi() {
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, false)) return false;
    prefs.remove("wifi_ssid");
    prefs.remove("wifi_pwd");
    prefs.putBool("configured", false);
    prefs.end();
    return true;
}
