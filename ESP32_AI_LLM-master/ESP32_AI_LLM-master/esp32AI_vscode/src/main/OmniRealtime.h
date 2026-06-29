/* OmniRealtime.h - Qwen3.5-Omni 实时语音对话模块 */
#ifndef OMNI_REALTIME_H
#define OMNI_REALTIME_H

#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFiClientSecure.h>
#include "base64.h"
#include <freertos/semphr.h>

#define OMNI_INPUT_SAMPLE_RATE 16000
#define OMNI_OUTPUT_SAMPLE_RATE 16000
#define OMNI_AUDIO_CHUNK_SIZE 1280
#define OMNI_RING_BUF_SAMPLES 320000 // 20秒@16kHz (640KB PSRAM)

// WS帧操作码
#define WS_OPCODE_CONTINUATION 0x00
#define WS_OPCODE_TEXT 0x01
#define WS_OPCODE_BINARY 0x02
#define WS_OPCODE_CLOSE 0x08
#define WS_OPCODE_PING 0x09
#define WS_OPCODE_PONG 0x0A

typedef void (*OmniTextCallback)(const String &text);
enum OmniState
{
    OMNI_DISCONNECTED,
    OMNI_CONNECTING,
    OMNI_SESSION_READY,
    OMNI_LISTENING,
    OMNI_USER_SPEAKING,
    OMNI_PROCESSING,
    OMNI_SPEAKING,
    OMNI_ERROR
};

class OmniRealtime
{
public:
    OmniRealtime();
    void begin(const String &apiKey, const String &voice,
               const String &model = "qwen3.5-omni-flash-realtime",
               const String &instructions = "你是一个AI伙伴。");
    bool connect();
    void disconnect();
    bool isConnected();
    // AEC净化：用播放参考抵消麦克风回声，减少服务端VAD误触发
    void processAudio(int16_t *pcmData, size_t samples);
    void sendAudio(const uint8_t *pcmData, size_t len);
    void loop();
    OmniState getState() { return _state; }
    int sslAvailable() { return _ssl.available(); }
    bool isPlaying() { return _playTask != nullptr; }
    int16_t *getAecBuf() { return _aecBuf; }
    void initPlaybackI2S(uint8_t bclk, uint8_t lrc, uint8_t dout);
    void setVolume(uint8_t vol);
    void cancelResponse(); // 取消当前回复（打断用）
    void onUserText(OmniTextCallback cb);
    void onAssistantText(OmniTextCallback cb);
    void onWaitingForSpeech(OmniTextCallback cb);

private:
    WiFiClientSecure _ssl;
    String _apiKey, _voice, _model, _instructions;
    bool _wsConnected;

    OmniState _state;
    unsigned long _lastReconnectAttempt;
    static const unsigned long RECONNECT_DELAY = 3000;

    // 环形缓冲 + FreeRTOS同步
    int16_t *_ringBuf;
    volatile size_t _ringWritePos;
    volatile size_t _ringReadPos;
    portMUX_TYPE _bufLock = portMUX_INITIALIZER_UNLOCKED;
    SemaphoreHandle_t _dataSem;
    TaskHandle_t _playTask;
    uint8_t _playVolume;
    int16_t _aecBuf[640] = {0}; // AEC回声参考（40ms@16kHz）
    uint8_t *_decodeBuf = nullptr;

    String _userTextBuffer;
    String _assistantTextBuffer;

    OmniTextCallback _onUserText;
    OmniTextCallback _onAssistantText;
    OmniTextCallback _onWaitingForSpeech;

    uint8_t _i2sBclk, _i2sLrc, _i2sDout;
    bool _i2sInitialized;

    // ---- 网络优化 ----
    bool _wsConnect(const char *host, uint16_t port, const char *path);
    bool _wsSendFrame(uint8_t opcode, const uint8_t *data, size_t len, bool masked = true);
    bool _wsSend(const String &msg);
    bool _wsSendBinary(const uint8_t *data, size_t len);
    bool _wsRecv(String &msg);
    void _wsClose();

    unsigned long _lastPingMs = 0;
    static const unsigned long PING_INTERVAL = 10000;
    bool _wsSendPing();

    uint16_t _reconnectDelay = 1000;
    static const uint16_t RECONNECT_DELAY_MAX = 30000;

    IPAddress _cachedIP;
    bool _dnsCached = false;

    String _audioFramePrefix;
    String _audioFrameSuffix;

    // ---- AEC ----
    float _aecFilter[256] = {0};
    int _aecFilterOrder = 128;
    float _aecGain = 0.01f;
    size_t _aecWriteIdx = 0; // _aecBuf循环写入位置（play task更新，AEC读取）
    uint16_t _prebufferMs = 200;         // 预缓冲(ms)
    unsigned long _lastDeltaTime = 0;
    uint16_t _maxDeltaGap = 0;
    static const uint16_t PREBUFFER_MIN = 200;
    static const uint16_t PREBUFFER_MAX = 1000;

    // ---- 自适应预缓冲 ----

    // ---- 核心方法 ----
    void _handleMessage(const char *jsonStr);
    void _sendSessionUpdate();
    void _processAudioDelta(const char *base64Audio);
    void _changeState(OmniState s)
    {
        if (_state != s)
        {
            _state = s;
            // 离开SPEAKING时清AEC参考
            if (_state == OMNI_SPEAKING && s != OMNI_SPEAKING)
            {
                memset(_aecBuf, 0, sizeof(_aecBuf));
                _aecWriteIdx = 0;
            }
        }
    }
    void _startPlayTask();
    static void _connectTaskFunc(void *param);
    static void _playTaskFunc(void *param);
};

#endif
