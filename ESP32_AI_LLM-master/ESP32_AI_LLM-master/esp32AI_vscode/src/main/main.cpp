/* ESP32 relay client - server keeps DashScope API Key and voice ID */
#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <driver/i2s.h>
#include "Audio1.h"
#include "ConfigStorage.h"

#define I2S_DOUT      4
#define I2S_BCLK      5
#define I2S_LRC       6
#define PIN_I2S_BCLK  2
#define PIN_I2S_LRC   1
#define PIN_I2S_DIN   42
#define LED_PIN       8
#define BUTTON_PIN    0

#define BUF_SAMPLES 64000
#define AUDIO_FRAME_BYTES 1280
#define AUDIO_HEADER_MAGIC 0x4455414EUL  // "NAUD" little-endian
#define AUDIO_HEADER_SIZE 16
#define AUDIO_TYPE_TTS 1
#define AUDIO_TYPE_MUSIC 2
#define AUDIO_SAMPLE_RATE 16000
#define TTS_START_BUFFER_SAMPLES 2560      // 160ms, low latency voice reply
#define MUSIC_START_BUFFER_SAMPLES 11200   // 700ms, stable music playback
#define MUSIC_LOW_BUFFER_SAMPLES 3200      // 200ms, rebuffer before choppy underrun

// Soft barge-in keeps a lightweight local mic gate active while TTS is playing.
// This is not full AEC; it detects louder near-field speech and then aborts playback.
#define ENABLE_SOFT_BARGE_IN 0
#define BARGE_RMS_THRESHOLD 1200
#define BARGE_MIN_FRAMES 4
#define BARGE_COOLDOWN_MS 900
#define SPEAKING_MIC_SUPPRESS_MS 350

Audio1 audio1(PIN_I2S_BCLK, PIN_I2S_LRC, PIN_I2S_DIN);
ConfigStorage configStore;
DeviceConfig cfg;
WebServer* server = nullptr;
WebSocketsClient ws;

enum VoiceState {
    STATE_IDLE,
    STATE_LISTENING,
    STATE_SPEAKING,
};

bool apMode = false;
bool wifiOk = false;
bool relayOk = false;
bool recordingEnabled = false;
uint8_t speakerVolume = 100;
bool interruptEnabled = false;
unsigned long lastReconnectMs = 0;
unsigned long lastButtonMs = 0;
bool lastButtonLevel = HIGH;
String sessionId;
String lastRelayError;
String lastRelayStatus;
unsigned long wsConnectedAtMs = 0;
uint32_t wsConnectCount = 0;
uint32_t wsDisconnectCount = 0;
VoiceState voiceState = STATE_IDLE;
unsigned long ttsStartedMs = 0;
unsigned long lastBargeInMs = 0;
uint8_t bargeHitFrames = 0;
unsigned long lastBufferReportMs = 0;
unsigned long lastBufferPrintMs = 0;
unsigned long lastAudioRxMs = 0;
uint32_t audioPacketsRx = 0;
uint32_t audioBytesRx = 0;
uint32_t audioUnderflows = 0;
uint32_t audioOverruns = 0;
uint32_t lastAudioSeq = 0;
uint8_t lastAudioType = 0;
bool audioHeaderSeen = false;
bool playbackActive = false;
bool playbackPrimed = false;
uint32_t jitterRebuffers = 0;

int16_t* ringBuf = nullptr;
volatile size_t wpos = 0;
volatile size_t rpos = 0;
TaskHandle_t playTask = nullptr;
portMUX_TYPE bufLock = portMUX_INITIALIZER_UNLOCKED;

size_t bufferedSamplesUnsafe() {
    return (wpos >= rpos) ? (wpos - rpos) : (BUF_SAMPLES - rpos + wpos);
}

size_t bufferedSamples() {
    size_t avail;
    portENTER_CRITICAL(&bufLock);
    avail = bufferedSamplesUnsafe();
    portEXIT_CRITICAL(&bufLock);
    return avail;
}

uint16_t bufferedMsFromSamples(size_t samples) {
    return (uint16_t)((samples * 1000UL) / AUDIO_SAMPLE_RATE);
}

uint8_t bufferedPctFromSamples(size_t samples) {
    size_t pct = (samples * 100UL) / BUF_SAMPLES;
    if (pct > 100) pct = 100;
    return (uint8_t)pct;
}

const char* audioTypeName(uint8_t type) {
    if (type == AUDIO_TYPE_TTS) return "tts";
    if (type == AUDIO_TYPE_MUSIC) return "music";
    return "raw";
}

const char* stateName() {
    switch (voiceState) {
        case STATE_LISTENING: return "listening";
        case STATE_SPEAKING: return "speaking";
        default: return "idle";
    }
}


void resetTripleResetCount() {
    Preferences nvs;
    if (!nvs.begin("rst_cnt", false)) return;
    nvs.putInt("count", 0);
    nvs.end();
}

bool shouldEnterApByTripleReset() {
    Preferences nvs;
    if (!nvs.begin("rst_cnt", false)) return false;
    int count = nvs.getInt("count", 0) + 1;
    nvs.putInt("count", count);
    nvs.end();

    Serial.printf("RST count: %d/3\n", count);
    if (count >= 3) {
        Serial.println("3x RST -> AP mode");
        resetTripleResetCount();
        return true;
    }
    return false;
}

void clearTripleResetCountWhenStable() {
    static bool cleared = false;
    if (cleared || millis() <= 5000) return;
    resetTripleResetCount();
    cleared = true;
}

uint32_t audioFrameRms(const int16_t* samples, size_t count) {
    uint64_t sum = 0;
    for (size_t i = 0; i < count; i++) {
        int32_t s = samples[i];
        sum += (uint32_t)(s * s);
    }
    return count > 0 ? (uint32_t)sqrt((double)sum / count) : 0;
}

void sendCors() {
    server->sendHeader("Access-Control-Allow-Origin", "*");
    server->sendHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    server->sendHeader("Access-Control-Allow-Headers", "Content-Type");
}

void initPlaybackI2S() {
    i2s_config_t c = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
        .sample_rate = 16000,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 8,
        .dma_buf_len = 256,
        .use_apll = 0,
        .tx_desc_auto_clear = true,
        .fixed_mclk = I2S_PIN_NO_CHANGE
    };
    i2s_pin_config_t p = {
        .bck_io_num = I2S_BCLK,
        .ws_io_num = I2S_LRC,
        .data_out_num = I2S_DOUT,
        .data_in_num = I2S_PIN_NO_CHANGE
    };
    i2s_driver_install(I2S_NUM_1, &c, 0, nullptr);
    i2s_set_pin(I2S_NUM_1, &p);
    ringBuf = (int16_t*)ps_malloc(BUF_SAMPLES * sizeof(int16_t));
    if (!ringBuf) ringBuf = (int16_t*)malloc(BUF_SAMPLES * sizeof(int16_t));
    if (ringBuf) memset(ringBuf, 0, BUF_SAMPLES * sizeof(int16_t));
}

void clearAudioBuffer() {
    portENTER_CRITICAL(&bufLock);
    wpos = 0;
    rpos = 0;
    if (ringBuf) memset(ringBuf, 0, BUF_SAMPLES * sizeof(int16_t));
    portEXIT_CRITICAL(&bufLock);
    playbackActive = false;
    lastAudioRxMs = 0;
    audioPacketsRx = 0;
    audioBytesRx = 0;
    audioUnderflows = 0;
    audioOverruns = 0;
    lastAudioSeq = 0;
    lastAudioType = 0;
    audioHeaderSeen = false;
    playbackPrimed = false;
    jitterRebuffers = 0;
}

void playTaskFunc(void*) {
    int16_t stereo[256];
    while (playTask) {
        size_t avail;
        bool takeAudio = false;
        bool becamePrimed = false;
        bool rebuffered = false;

        portENTER_CRITICAL(&bufLock);
        avail = (wpos >= rpos) ? (wpos - rpos) : (BUF_SAMPLES - rpos + wpos);
        size_t startThreshold = (lastAudioType == AUDIO_TYPE_MUSIC) ? MUSIC_START_BUFFER_SAMPLES : TTS_START_BUFFER_SAMPLES;
        if (playbackActive && !playbackPrimed && avail >= startThreshold) {
            playbackPrimed = true;
            becamePrimed = true;
        }
        if (playbackActive && playbackPrimed && lastAudioType == AUDIO_TYPE_MUSIC && avail < MUSIC_LOW_BUFFER_SAMPLES) {
            playbackPrimed = false;
            jitterRebuffers++;
            audioUnderflows++;
            rebuffered = true;
        }
        takeAudio = playbackActive && playbackPrimed && avail >= 128 && ringBuf;
        if (takeAudio) {
            for (size_t i = 0; i < 128; i++) {
                int32_t s = ringBuf[rpos];
                s = (s * speakerVolume) / 100;
                stereo[i * 2] = (int16_t)s;
                stereo[i * 2 + 1] = (int16_t)s;
                rpos = (rpos + 1) % BUF_SAMPLES;
            }
        }
        portEXIT_CRITICAL(&bufLock);

        if (becamePrimed) {
            Serial.printf("[Jitter ready] type=%s buf=%ums/%u%%\n", audioTypeName(lastAudioType), bufferedMsFromSamples(avail), bufferedPctFromSamples(avail));
        }
        if (rebuffered) {
            Serial.printf("[Jitter rebuffer] type=%s buf=%ums rebuf=%u\n", audioTypeName(lastAudioType), bufferedMsFromSamples(avail), jitterRebuffers);
        }

        size_t written;
        if (takeAudio) {
            i2s_write(I2S_NUM_1, stereo, sizeof(stereo), &written, portMAX_DELAY);
        } else {
            if (playbackActive && playbackPrimed && (millis() - lastAudioRxMs < 1500 || avail > 0)) audioUnderflows++;
            memset(stereo, 0, sizeof(stereo));
            i2s_write(I2S_NUM_1, stereo, sizeof(stereo), &written, portMAX_DELAY);
            vTaskDelay(1);
        }
    }
    vTaskDelete(nullptr);
}

void startPlayTask() {
    if (!playTask) xTaskCreatePinnedToCore(playTaskFunc, "play", 4096, nullptr, 1, &playTask, 1);
}

void sendDoc(JsonDocument& doc) {
    if (sessionId.length() > 0 && !doc.containsKey("session_id")) doc["session_id"] = sessionId;
    String out;
    serializeJson(doc, out);
    ws.sendTXT(out);
}

void sendHello() {
    StaticJsonDocument<384> doc;
    doc["type"] = "hello";
    doc["version"] = 1;
    doc["transport"] = "websocket";
    JsonObject features = doc.createNestedObject("features");
    features["mcp"] = true;
    features["interrupt"] = true;
    features["audio_header"] = true;
    features["buffer_status"] = true;
    JsonObject audio = doc.createNestedObject("audio_params");
    audio["format"] = "pcm";
    audio["sample_rate"] = 16000;
    audio["channels"] = 1;
    audio["frame_duration"] = 40;
    sendDoc(doc);
}


void applyRuntimeConfig(JsonObject data) {
    if (data.containsKey("volume")) {
        speakerVolume = constrain((int)(data["volume"] | speakerVolume), 0, 100);
    }
    if (data.containsKey("interrupt_enabled")) {
        interruptEnabled = data["interrupt_enabled"] | interruptEnabled;
    }
    JsonObject features = data["features"];
    if (!features.isNull() && features.containsKey("interrupt")) {
        interruptEnabled = features["interrupt"] | interruptEnabled;
    }
    Serial.printf("[Config] volume=%u interrupt=%s\n", speakerVolume, interruptEnabled ? "on" : "off");
}

void sendListen(const char* state, const char* mode = "auto") {
    StaticJsonDocument<192> doc;
    doc["type"] = "listen";
    doc["state"] = state;
    if (strcmp(state, "start") == 0) doc["mode"] = mode;
    sendDoc(doc);
}

void startListening() {
    if (!relayOk) return;
    voiceState = STATE_LISTENING;
    recordingEnabled = true;
    bargeHitFrames = 0;
    sendListen("start", "auto");
    Serial.println("[State] listening");
}

void stopListening() {
    if (!relayOk) return;
    recordingEnabled = false;
    voiceState = STATE_IDLE;
    sendListen("stop");
    Serial.println("[State] idle");
}

void sendAbort(const char* reason) {
    if (!relayOk) return;
    StaticJsonDocument<192> doc;
    doc["type"] = "abort";
    doc["reason"] = reason;
    sendDoc(doc);
}

void abortAndListen(const char* reason) {
    sendAbort(reason);
    clearAudioBuffer();
    startListening();
}

void sendMcpPayload(const String& payload) {
    String out = "{\"type\":\"mcp\"";
    if (sessionId.length() > 0) out += ",\"session_id\":\"" + sessionId + "\"";
    out += ",\"payload\":" + payload + "}";
    ws.sendTXT(out);
}

void sendMcpResult(int id, const String& resultJson) {
    sendMcpPayload("{\"jsonrpc\":\"2.0\",\"id\":" + String(id) + ",\"result\":" + resultJson + "}");
}

void sendMcpTextResult(int id, const String& text, bool isError = false) {
    String escaped = text;
    escaped.replace("\\", "\\\\");
    escaped.replace("\"", "\\\"");
    String result = "{\"content\":[{\"type\":\"text\",\"text\":\"" + escaped + "\"}],\"isError\":" + String(isError ? "true" : "false") + "}";
    sendMcpResult(id, result);
}

void sendMcpError(int id, const String& message) {
    String escaped = message;
    escaped.replace("\\", "\\\\");
    escaped.replace("\"", "\\\"");
    sendMcpPayload("{\"jsonrpc\":\"2.0\",\"id\":" + String(id) + ",\"error\":{\"code\":-32601,\"message\":\"" + escaped + "\"}}");
}

String toolsListJson() {
    return "{\"tools\":["
        "{\"name\":\"self.get_device_status\",\"description\":\"Get ESP32 status including WiFi, relay, state and volume.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{}}},"
        "{\"name\":\"self.audio_speaker.set_volume\",\"description\":\"Set ESP32 speaker output volume from 0 to 100.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"volume\":{\"type\":\"integer\",\"minimum\":0,\"maximum\":100}},\"required\":[\"volume\"]}},"
        "{\"name\":\"self.audio.clear_buffer\",\"description\":\"Clear queued speaker audio immediately.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{}}},"
        "{\"name\":\"self.listen.start\",\"description\":\"Start microphone listening.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{}}},"
        "{\"name\":\"self.listen.stop\",\"description\":\"Stop microphone listening.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{}}},"
        "{\"name\":\"self.device.reboot\",\"description\":\"Reboot ESP32.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{}}}"
        "],\"nextCursor\":\"\"}";
}

String deviceStatusJson() {
    String json = "{";
    json += "\"state\":\"" + String(stateName()) + "\",";
    json += "\"wifi_connected\":" + String(WiFi.status() == WL_CONNECTED ? "true" : "false") + ",";
    json += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
    json += "\"relay_host\":\"" + cfg.relayHost + "\",";
    json += "\"relay_port\":" + String(cfg.relayPort) + ",";
    size_t buf = bufferedSamples();
    json += "\"speaker_volume\":" + String(speakerVolume) + ",";
    json += "\"buffer_ms\":" + String(bufferedMsFromSamples(buf)) + ",";
    json += "\"buffer_pct\":" + String(bufferedPctFromSamples(buf)) + ",";
    json += "\"underflows\":" + String(audioUnderflows) + ",";
    json += "\"overruns\":" + String(audioOverruns) + ",";
    json += "\"recording\":" + String(recordingEnabled ? "true" : "false");
    json += "}";
    return json;
}

void handleMcp(JsonObject payload) {
    String method = payload["method"] | "";
    int id = payload["id"] | 0;
    JsonObject params = payload["params"];

    if (method == "initialize") {
        sendMcpResult(id, "{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"esp32-pio-relay\",\"version\":\"1.0.0\"}}");
        return;
    }
    if (method == "tools/list") {
        sendMcpResult(id, toolsListJson());
        return;
    }
    if (method != "tools/call") {
        sendMcpError(id, "Method not implemented: " + method);
        return;
    }

    String name = params["name"] | "";
    JsonObject args = params["arguments"];
    if (name == "self.get_device_status") {
        sendMcpTextResult(id, deviceStatusJson());
    } else if (name == "self.audio_speaker.set_volume") {
        int volume = args["volume"] | speakerVolume;
        speakerVolume = constrain(volume, 0, 100);
        sendMcpTextResult(id, "true");
    } else if (name == "self.audio.clear_buffer") {
        clearAudioBuffer();
        sendMcpTextResult(id, "true");
    } else if (name == "self.listen.start") {
        startListening();
        sendMcpTextResult(id, "true");
    } else if (name == "self.listen.stop") {
        stopListening();
        sendMcpTextResult(id, "true");
    } else if (name == "self.device.reboot") {
        sendMcpTextResult(id, "true");
        delay(300);
        ESP.restart();
    } else {
        sendMcpError(id, "Unknown tool: " + name);
    }
}

void handleControlCommand(const String& cmd) {
    if (cmd == "stop_record") {
        recordingEnabled = false;
    } else if (cmd == "start_record") {
        if (voiceState == STATE_SPEAKING) {
            recordingEnabled = interruptEnabled;
        } else {
            recordingEnabled = true;
            voiceState = STATE_LISTENING;
        }
    } else if (cmd == "clear_audio") {
        clearAudioBuffer();
    }
}

String payloadPreview(uint8_t* payload, size_t length, size_t maxLen = 180) {
    size_t n = length < maxLen ? length : maxLen;
    String out;
    out.reserve(n + 4);
    for (size_t i = 0; i < n; i++) {
        char c = (char)payload[i];
        out += (c >= 32 && c <= 126) ? c : '.';
    }
    if (length > maxLen) out += "...";
    return out;
}

uint16_t readLe16(const uint8_t* p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

uint32_t readLe32(const uint8_t* p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

void pushPcmToRing(const uint8_t* data, size_t length) {
    if (!ringBuf || length < 2) return;
    const int16_t* samples = (const int16_t*)data;
    size_t count = length / 2;
    portENTER_CRITICAL(&bufLock);
    for (size_t i = 0; i < count; i++) {
        ringBuf[wpos] = samples[i];
        wpos = (wpos + 1) % BUF_SAMPLES;
        if (wpos == rpos) {
            rpos = (rpos + 1) % BUF_SAMPLES;
            audioOverruns++;
        }
    }
    portEXIT_CRITICAL(&bufLock);
    audioPacketsRx++;
    audioBytesRx += length;
    lastAudioRxMs = millis();
    playbackActive = true;
}

void handleAudioBin(uint8_t* payload, size_t length) {
    if (length < AUDIO_HEADER_SIZE || readLe32(payload) != AUDIO_HEADER_MAGIC) {
        Serial.printf("[Audio reject] missing NAUD header len=%u; update server/app\n", (unsigned)length);
        return;
    }

    uint8_t version = payload[4];
    uint8_t audioType = payload[5];
    uint16_t headerLen = readLe16(payload + 6);
    uint32_t seq = readLe32(payload + 8);
    uint32_t timestampMs = readLe32(payload + 12);

    if (version != 1 || headerLen < AUDIO_HEADER_SIZE || headerLen > length) {
        Serial.printf("[Audio reject] bad header version=%u header=%u len=%u\n", version, headerLen, (unsigned)length);
        return;
    }
    if (audioType != AUDIO_TYPE_TTS && audioType != AUDIO_TYPE_MUSIC) {
        Serial.printf("[Audio reject] unknown type=%u seq=%u\n", audioType, seq);
        return;
    }

    audioHeaderSeen = true;
    if (lastAudioType != 0 && lastAudioType != audioType) {
        playbackPrimed = false;
    }
    lastAudioType = audioType;
    if (lastAudioSeq != 0 && seq != lastAudioSeq + 1) {
        Serial.printf("[Audio seq gap] type=%s last=%u now=%u\n", audioTypeName(audioType), lastAudioSeq, seq);
    }
    lastAudioSeq = seq;
    pushPcmToRing(payload + headerLen, length - headerLen);
    size_t buf = bufferedSamples();
    if (audioPacketsRx % 80 == 1) {
        Serial.printf("[Audio packet] type=%s seq=%u ts=%u payload=%u buf=%ums/%u%%\n", audioTypeName(audioType), seq, timestampMs, (unsigned)(length - headerLen), bufferedMsFromSamples(buf), bufferedPctFromSamples(buf));
    }
}

void sendBufferStatus(bool force = false) {
    if (!relayOk) return;
    unsigned long now = millis();
    if (!force && now - lastBufferReportMs < 200) return;
    lastBufferReportMs = now;
    size_t buf = bufferedSamples();
    StaticJsonDocument<320> doc;
    doc["type"] = "buffer_status";
    doc["buffer_samples"] = (uint32_t)buf;
    doc["buffer_ms"] = bufferedMsFromSamples(buf);
    doc["buffer_pct"] = bufferedPctFromSamples(buf);
    doc["free_samples"] = (uint32_t)(BUF_SAMPLES - buf);
    doc["underflows"] = audioUnderflows;
    doc["overruns"] = audioOverruns;
    doc["packets"] = audioPacketsRx;
    doc["bytes"] = audioBytesRx;
    doc["audio_header"] = audioHeaderSeen;
    doc["primed"] = playbackPrimed;
    doc["rebuffer_count"] = jitterRebuffers;
    doc["last_type"] = audioTypeName(lastAudioType);
    doc["last_seq"] = lastAudioSeq;
    doc["rssi"] = WiFi.RSSI();
    sendDoc(doc);
    if (now - lastBufferPrintMs >= 1000) {
        lastBufferPrintMs = now;
        Serial.printf("[Jitter] buf=%ums/%u%% free=%u primed=%d rebuf=%u under=%u over=%u pkts=%u type=%s seq=%u rssi=%d\n", bufferedMsFromSamples(buf), bufferedPctFromSamples(buf), (unsigned)(BUF_SAMPLES - buf), playbackPrimed ? 1 : 0, jitterRebuffers, audioUnderflows, audioOverruns, audioPacketsRx, audioTypeName(lastAudioType), lastAudioSeq, WiFi.RSSI());
    }
}

void handleRelayText(uint8_t* payload, size_t length) {
    StaticJsonDocument<4096> doc;
    DeserializationError jsonErr = deserializeJson(doc, payload, length);
    if (jsonErr) {
        Serial.printf("[Relay JSON error] %s len=%u raw=%s\n", jsonErr.c_str(), (unsigned)length, payloadPreview(payload, length).c_str());
        return;
    }

    String relayType = doc["relay_type"] | "";
    if (relayType == "control") {
        handleControlCommand(doc["command"] | "");
        return;
    }

    String type = doc["type"] | "";
    if (type == "config") {
        applyRuntimeConfig(doc.as<JsonObject>());
        return;
    }
    if (type == "hello") {
        sessionId = doc["session_id"] | sessionId;
        applyRuntimeConfig(doc.as<JsonObject>());
        if (voiceState == STATE_IDLE) startListening();
        Serial.printf("[Server hello] session=%s\n", sessionId.c_str());
        return;
    }
    if (type == "status") {
        String state = doc["state"] | "";
        String message = doc["message"] | "";
        lastRelayStatus = state + (message.length() ? (": " + message) : "");
        Serial.printf("[Relay status] %s%s%s\n", state.c_str(), message.length() ? " - " : "", message.c_str());
        if (state == "music_start") playbackActive = true;
        if (state == "music_stop" || state == "music_cancelled" || state == "music_interrupted") playbackActive = false;
        return;
    }
    if (type == "control") {
        handleControlCommand(doc["command"] | "");
        return;
    }
    if (type == "abort") {
        clearAudioBuffer();
        if (relayOk) startListening();
        return;
    }
    if (type == "tts") {
        String state = doc["state"] | "";
        if (state == "start") {
            voiceState = STATE_SPEAKING;
            recordingEnabled = interruptEnabled;
            ttsStartedMs = millis();
            bargeHitFrames = 0;
            clearAudioBuffer();
            playbackActive = true;
            Serial.println("[TTS] start");
        } else if (state == "stop") {
            playbackActive = false;
            Serial.println("[TTS] stop");
            if (relayOk) startListening();
        } else if (state == "sentence_start") {
            Serial.printf("{\"type\":\"assistant\",\"text\":\"%s\"}\n", (const char*)(doc["text"] | ""));
        }
        return;
    }
    if (type == "stt") {
        Serial.printf("{\"type\":\"user\",\"text\":\"%s\"}\n", (const char*)(doc["text"] | ""));
        return;
    }
    if (type == "llm") {
        Serial.printf("{\"type\":\"assistant_delta\",\"delta\":\"%s\"}\n", (const char*)(doc["text_delta"] | ""));
        return;
    }
    if (type == "mcp") {
        JsonObject mcpPayload = doc["payload"];
        if (!mcpPayload.isNull()) handleMcp(mcpPayload);
        return;
    }
    if (type == "error" || relayType == "error") {
        lastRelayError = doc["message"] | "";
        Serial.printf("[Relay error] %s\n", lastRelayError.c_str());
        return;
    }
}

void onWs(WStype_t type, uint8_t* payload, size_t length) {
    if (type == WStype_CONNECTED) {
        relayOk = true;
        recordingEnabled = false;
        voiceState = STATE_IDLE;
        lastRelayError = "";
        lastRelayStatus = "";
        wsConnectedAtMs = millis();
        wsConnectCount++;
        clearAudioBuffer();
        startPlayTask();
        Serial.printf("[WS] relay connected #%u ip=%s rssi=%d\n", wsConnectCount, WiFi.localIP().toString().c_str(), WiFi.RSSI());
        sendHello();
        sendBufferStatus(true);
    } else if (type == WStype_DISCONNECTED) {
        unsigned long aliveMs = wsConnectedAtMs ? (millis() - wsConnectedAtMs) : 0;
        wsDisconnectCount++;
        relayOk = false;
        recordingEnabled = false;
        voiceState = STATE_IDLE;
        Serial.printf("[WS] relay disconnected #%u alive_ms=%lu wifi=%d rssi=%d\n", wsDisconnectCount, aliveMs, (int)WiFi.status(), WiFi.RSSI());
        if (lastRelayStatus.length()) Serial.printf("[WS] last status: %s\n", lastRelayStatus.c_str());
        if (lastRelayError.length()) Serial.printf("[WS] last error: %s\n", lastRelayError.c_str());
        if (length > 0) Serial.printf("[WS] close payload: %s\n", payloadPreview(payload, length).c_str());
    } else if (type == WStype_ERROR) {
        lastRelayError = length > 0 ? payloadPreview(payload, length) : "websocket error";
        Serial.printf("[WS error] %s\n", lastRelayError.c_str());
    } else if (type == WStype_TEXT) {
        handleRelayText(payload, length);
    } else if (type == WStype_BIN) {
        handleAudioBin(payload, length);
    }
}

bool startWiFi(const String& ssid, const String& password) {
    if (ssid.length() == 0) return false;
    WiFi.disconnect(true);
    delay(100);
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid.c_str(), password.c_str());
    Serial.printf("WiFi %s...", ssid.c_str());
    for (int i = 0; i < 150 && WiFi.status() != WL_CONNECTED; i++) {
        delay(100);
        Serial.print(".");
        digitalWrite(LED_PIN, i % 2);
    }
    bool ok = WiFi.status() == WL_CONNECTED;
    digitalWrite(LED_PIN, ok ? LOW : HIGH);
    Serial.println(ok ? " OK" : " FAIL");
    if (ok) Serial.println(WiFi.localIP());
    return ok;
}


String relayUrlForDisplay() {
    String path = cfg.relayPath.length() ? cfg.relayPath : "/";
    return "ws://" + cfg.relayHost + ":" + String(cfg.relayPort) + path;
}

bool parseRelayUrl(String input, String& host, uint16_t& port, String& path) {
    input.trim();
    if (input.length() == 0) return false;
    input.replace(" ", "");
    if (input.startsWith("ws://")) input = input.substring(5);
    if (input.startsWith("http://")) input = input.substring(7);
    if (input.startsWith("wss://") || input.startsWith("https://")) return false;

    int slash = input.indexOf('/');
    String authority = slash >= 0 ? input.substring(0, slash) : input;
    path = slash >= 0 ? input.substring(slash) : "/";
    if (path.length() == 0) path = "/";

    int colon = authority.lastIndexOf(':');
    if (colon >= 0) {
        host = authority.substring(0, colon);
        int parsedPort = authority.substring(colon + 1).toInt();
        port = parsedPort > 0 ? parsedPort : DEFAULT_RELAY_PORT;
    } else {
        host = authority;
        port = DEFAULT_RELAY_PORT;
    }

    host.trim();
    if (host.length() == 0) return false;
    if (!path.startsWith("/")) path = "/" + path;
    return true;
}

String htmlEscape(String value) {
    value.replace("&", "&amp;");
    value.replace("\"", "&quot;");
    value.replace("<", "&lt;");
    value.replace(">", "&gt;");
    return value;
}


String relayPathForLog(String path) {
    int tokenPos = path.indexOf("token=");
    if (tokenPos < 0) return path;
    int tokenEnd = path.indexOf('&', tokenPos);
    if (tokenEnd < 0) tokenEnd = path.length();
    return path.substring(0, tokenPos) + "token=***" + path.substring(tokenEnd);
}

bool isEspApTempHost(const String& host) {
    return host.startsWith("192.168.4.");
}

void connectRelay() {
    if (!wifiOk || cfg.relayHost.length() == 0) return;
    String relayPath = cfg.relayPath.length() ? cfg.relayPath : "/";
    String logPath = relayPathForLog(relayPath);
    Serial.printf("Relay ws://%s:%u%s\n", cfg.relayHost.c_str(), cfg.relayPort, logPath.c_str());
    ws.onEvent(onWs);
    ws.begin(cfg.relayHost.c_str(), cfg.relayPort, relayPath.c_str());
    ws.setReconnectInterval(3000);
}

void startAPMode() {
    apMode = true;
    WiFi.mode(WIFI_AP);
    WiFi.softAPConfig(IPAddress(192,168,4,1), IPAddress(192,168,4,1), IPAddress(255,255,255,0));
    WiFi.softAP("ESP32_Voice_Config");
    Serial.println("AP: ESP32_Voice_Config");

    server = new WebServer(80);
    server->on("/", []() {
        String html =
            "<!DOCTYPE html><html><head><meta charset=UTF-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>ESP32 Relay Config</title>"
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#eaf7ff;padding:20px;color:#17324d}"
            ".c{max-width:420px;margin:0 auto;background:#f8fcff;border:1px solid #b8def1;border-radius:8px;padding:22px}"
            "h2{margin-top:0}label{display:block;margin-top:14px;font-weight:700;font-size:14px}"
            "input{width:100%;padding:10px;border:1px solid #9bcfe8;border-radius:8px;margin-top:5px;box-sizing:border-box}"
            "button{width:100%;padding:13px;background:#1b8fd2;color:white;border:none;border-radius:8px;font-size:16px;font-weight:700;margin-top:20px}"
            ".hint{color:#5b7892;font-size:13px;line-height:1.5}</style></head><body><div class=c>"
            "<h2>ESP32 中转配置</h2><div class=hint>这里只保存 WiFi 和服务端中转地址，不保存 API Key 和音色 ID。</div>"
            "<form id=f><label>WiFi 名称<input id=s value='" + cfg.wifiSsid + "'></label>"
            "<label>WiFi 密码<input type=password id=p value='" + cfg.wifiPassword + "'></label>"
            "<label>服务端 WebSocket 地址<input id=u placeholder='ws://手机IP:8765/?token=...' value='" + htmlEscape(relayUrlForDisplay()) + "'></label>"
            "<div class=hint>直接粘贴手机 App 里显示的 ESP32 中转地址，必须保留 ?token=...。不要填写 192.168.4.x，这是配网临时地址。</div>"
            "<button type=submit>保存并重启</button></form><div id=st class=hint style='margin-top:12px'></div>"
            "<script>document.getElementById('f').onsubmit=async function(e){e.preventDefault();let b=document.querySelector('button');b.disabled=true;b.textContent='保存中...';try{let res=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wifi_ssid:document.getElementById('s').value,wifi_password:document.getElementById('p').value,relay_url:document.getElementById('u').value})});let j={};try{j=await res.json()}catch(_){}document.getElementById('st').textContent=res.ok?'配置已保存，正在重启':(j.error||'保存失败');b.disabled=false;b.textContent='保存并重启';}catch(e){document.getElementById('st').textContent='保存失败: '+e.message;b.disabled=false;b.textContent='保存并重启'}}</script>"
            "</div></body></html>";
        sendCors();
        server->send(200, "text/html", html);
    });

    server->on("/api/config", HTTP_OPTIONS, []() { sendCors(); server->send(204); });
    server->on("/api/config", HTTP_POST, []() {
        String body = server->arg("plain");
        StaticJsonDocument<768> doc;
        if (deserializeJson(doc, body)) {
            sendCors();
            server->send(400, "application/json", "{\"ok\":false}");
            return;
        }
        cfg = configStore.load();
        String wifiSsid = doc["wifi_ssid"] | "";
        String wifiPassword = doc["wifi_password"] | "";
        String relayUrl = doc["relay_url"] | "";
        String relayHost = doc["relay_host"] | "";
        int relayPort = doc["relay_port"] | DEFAULT_RELAY_PORT;
        String relayPath = doc["relay_path"] | "/";
        if (wifiSsid.length() > 0) cfg.wifiSsid = wifiSsid;
        cfg.wifiPassword = wifiPassword;
        if (relayUrl.length() > 0) {
            if (!parseRelayUrl(relayUrl, relayHost, (uint16_t&)relayPort, relayPath)) {
                sendCors();
                server->send(400, "application/json", "{\"ok\":false,\"error\":\"bad relay url\"}");
                return;
            }
        }
        if (isEspApTempHost(relayHost)) {
            sendCors();
            server->send(400, "application/json", "{\"ok\":false,\"error\":\"不要填写 192.168.4.x，这是 ESP32 配网临时地址。请填写手机/电脑在最终 WiFi 或热点里的服务端地址\"}");
            return;
        }
        if (relayHost.length() > 0) cfg.relayHost = relayHost;
        cfg.relayPort = constrain(relayPort, 1, 65535);
        cfg.relayPath = relayPath.length() ? relayPath : "/";
        cfg.isConfigured = true;
        configStore.save(cfg);
        sendCors();
        server->send(200, "application/json", "{\"ok\":true}");
        delay(800);
        ESP.restart();
    });

    server->begin();
}

void handleButton() {
    bool level = digitalRead(BUTTON_PIN);
    unsigned long now = millis();
    if (level != lastButtonLevel && now - lastButtonMs > 40) {
        lastButtonMs = now;
        lastButtonLevel = level;
        if (level == LOW && relayOk) {
            if (voiceState == STATE_SPEAKING) {
                abortAndListen("button_pressed");
            } else if (voiceState == STATE_LISTENING) {
                stopListening();
            } else {
                startListening();
            }
        }
    }
}

void setup() {
    Serial.begin(115200);
    delay(300);
    pinMode(LED_PIN, OUTPUT);
    pinMode(BUTTON_PIN, INPUT_PULLUP);
    Serial.println("{\"status\":\"boot\",\"mode\":\"relay\"}");

    bool forceApMode = shouldEnterApByTripleReset();
    cfg = configStore.load();
    if (forceApMode) {
        startAPMode();
        return;
    }

    if (cfg.wifiSsid.length() == 0 || cfg.relayHost.length() == 0) {
        startAPMode();
        return;
    }

    audio1.init();
    initPlaybackI2S();
    wifiOk = startWiFi(cfg.wifiSsid, cfg.wifiPassword);
    if (!wifiOk) {
        startAPMode();
        return;
    }
    connectRelay();
}

void loop() {
    if (apMode) {
        server->handleClient();
        return;
    }

    clearTripleResetCountWhenStable();

    handleButton();
    ws.loop();
    sendBufferStatus(false);
    if (wifiOk && !relayOk && millis() - lastReconnectMs > 3000) {
        lastReconnectMs = millis();
        connectRelay();
    }

    if (relayOk && recordingEnabled && (voiceState == STATE_LISTENING || (interruptEnabled && voiceState == STATE_SPEAKING))) {
        audio1.Record();

        if (voiceState == STATE_SPEAKING) {
            if (interruptEnabled) {
                ws.sendBIN((uint8_t*)audio1.wavData[0], AUDIO_FRAME_BYTES);
            }
            return;
        }

        ws.sendBIN((uint8_t*)audio1.wavData[0], AUDIO_FRAME_BYTES);
    }
    delay(1);
}
