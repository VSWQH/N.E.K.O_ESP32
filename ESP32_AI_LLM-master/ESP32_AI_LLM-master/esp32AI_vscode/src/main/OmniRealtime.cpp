/* OmniRealtime.cpp - Qwen3.5-Omni 实时语音（服务端VAD + 本地AEC） */
#include "OmniRealtime.h"
#include <driver/i2s.h>

OmniRealtime::OmniRealtime()
    : _wsConnected(false), _state(OMNI_DISCONNECTED), _lastReconnectAttempt(0), _ringBuf(nullptr), _ringWritePos(0), _ringReadPos(0), _dataSem(nullptr), _playTask(nullptr), _playVolume(10), _onUserText(nullptr), _onAssistantText(nullptr), _onWaitingForSpeech(nullptr), _i2sBclk(5), _i2sLrc(6), _i2sDout(4), _i2sInitialized(false), _reconnectDelay(1000), _dnsCached(false), _lastPingMs(0)
{
}

// ===== 统一WebSocket帧发送 =====
bool OmniRealtime::_wsSendFrame(uint8_t opcode, const uint8_t *data, size_t len, bool masked)
{
    if (!_ssl.connected())
        return false;
    uint8_t frame[10];
    size_t hlen;
    frame[0] = 0x80 | opcode;
    if (masked)
    {
        if (len < 126)
        {
            frame[1] = 0x80 | (uint8_t)len;
            hlen = 2;
        }
        else if (len < 65536)
        {
            frame[1] = 0x80 | 126;
            frame[2] = (len >> 8) & 0xFF;
            frame[3] = len & 0xFF;
            hlen = 4;
        }
        else
        {
            frame[1] = 0x80 | 127;
            for (int i = 0; i < 8; i++)
                frame[2 + i] = (len >> (56 - 8 * i)) & 0xFF;
            hlen = 10;
        }
    }
    else
    {
        if (len < 126)
        {
            frame[1] = (uint8_t)len;
            hlen = 2;
        }
        else if (len < 65536)
        {
            frame[1] = 126;
            frame[2] = (len >> 8) & 0xFF;
            frame[3] = len & 0xFF;
            hlen = 4;
        }
        else
        {
            frame[1] = 127;
            for (int i = 0; i < 8; i++)
                frame[2 + i] = (len >> (56 - 8 * i)) & 0xFF;
            hlen = 10;
        }
    }
    _ssl.write(frame, hlen);
    if (masked)
    {
        uint8_t mask[4] = {0x12, 0x34, 0x56, 0x78};
        _ssl.write(mask, 4);
        if (len > 0)
        {
            uint8_t buf[512];
            size_t w = 0;
            while (w < len)
            {
                size_t c = (len - w < 512) ? (len - w) : 512;
                for (size_t i = 0; i < c; i++)
                    buf[i] = data[w + i] ^ mask[(w + i) % 4];
                _ssl.write(buf, c);
                w += c;
            }
        }
    }
    else if (len > 0)
    {
        _ssl.write(data, len);
    }
    return true;
}
bool OmniRealtime::_wsSend(const String &msg) { return _wsSendFrame(WS_OPCODE_TEXT, (const uint8_t *)msg.c_str(), msg.length(), true); }
bool OmniRealtime::_wsSendBinary(const uint8_t *data, size_t len) { return _wsSendFrame(WS_OPCODE_BINARY, data, len, true); }
bool OmniRealtime::_wsSendPing() { return _wsSendFrame(WS_OPCODE_PING, nullptr, 0, true); }

// ===== WebSocket over SSL =====
bool OmniRealtime::_wsConnect(const char *host, uint16_t port, const char *path)
{
    _ssl.stop();
    _ssl.setInsecure();
    _ssl.setTimeout(10000);
    Serial.printf("[WSS] %s:%d ...\n", host, port);
    if (!_ssl.connect(host, port))
    {
        Serial.println("[WSS] SSL连接失败");
        return false;
    }
    Serial.println("[WSS] SSL已连接");

    String key = base64::encode((const uint8_t *)"0123456789012345", 16);
    String req = "GET " + String(path) + " HTTP/1.1\r\nHost: " + String(host) + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n";
    req += "Sec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer " + _apiKey + "\r\n\r\n";
    _ssl.print(req);
    _ssl.flush();

    unsigned long t = millis();
    String response;
    while (millis() - t < 10000)
    {
        while (_ssl.available())
            response += (char)_ssl.read();
        if (response.indexOf("\r\n\r\n") != -1)
            break;
        delay(10);
    }
    if (response.indexOf("101") == -1)
    {
        Serial.printf("[WSS] 升级失败: %s\n", response.c_str());
        _ssl.stop();
        return false;
    }
    Serial.println("[WSS] WebSocket升级成功");
    return true;
}

bool OmniRealtime::_wsRecv(String &msg)
{
    if (!_ssl.connected() || _ssl.available() < 2)
        return false;
    uint8_t hdr[2];
    _ssl.readBytes(hdr, 2);
    uint8_t opcode = hdr[0] & 0x0F;
    bool masked = (hdr[1] & 0x80);
    uint64_t len = hdr[1] & 0x7F;
    if (len == 126)
    {
        uint8_t e[2];
        _ssl.readBytes(e, 2);
        len = ((uint64_t)e[0] << 8) | e[1];
    }
    else if (len == 127)
    {
        uint8_t e[8];
        _ssl.readBytes(e, 8);
        len = 0;
        for (int i = 0; i < 8; i++)
            len = (len << 8) | e[i];
    }
    uint8_t mkey[4] = {0};
    if (masked)
        _ssl.readBytes(mkey, 4);

    if (opcode == WS_OPCODE_PING)
    {
        if (len > 0)
        {
            uint8_t *d = (uint8_t *)malloc(len);
            if (d)
            {
                _ssl.readBytes(d, len);
                free(d);
            }
        }
        _wsSendFrame(WS_OPCODE_PONG, nullptr, 0, true);
        return false;
    }
    if (opcode == WS_OPCODE_PONG)
    {
        if (len > 0)
        {
            uint8_t *d = (uint8_t *)malloc(len);
            if (d)
            {
                _ssl.readBytes(d, len);
                free(d);
            }
        }
        return false;
    }
    if (len > 65536)
    {
        _ssl.stop();
        return false;
    }

    bool useStack = (len <= 512);
    uint8_t stackBuf[512];
    uint8_t *buf = useStack ? stackBuf : (uint8_t *)malloc(len + 1);
    if (!buf)
        return false;
    size_t total = 0;
    unsigned long t = millis();
    while (total < len && millis() - t < 10000)
    {
        if (_ssl.available())
            total += _ssl.readBytes(buf + total, len - total);
        else
            delay(1);
    }
    if (total < len)
    {
        if (!useStack)
            free(buf);
        return false;
    }
    buf[len] = 0;
    if (masked)
        for (size_t i = 0; i < len; i++)
            buf[i] ^= mkey[i % 4];
    bool isText = (opcode == WS_OPCODE_TEXT);
    bool isClose = (opcode == WS_OPCODE_CLOSE);
    if (isText)
        msg = String((char *)buf);
    else if (isClose)
        _ssl.stop();
    if (!useStack)
        free(buf);
    return isText;
}

void OmniRealtime::_wsClose()
{
    _wsConnected = false;
    if (_ssl.connected())
    {
        uint8_t p[] = {0x03, 0xE8};
        _wsSendFrame(WS_OPCODE_CLOSE, p, 2, true);
        _ssl.flush();
        delay(10);
    }
    _ssl.stop();
}

// ===== 后台连接任务 =====
void OmniRealtime::_connectTaskFunc(void *param)
{
    OmniRealtime *self = (OmniRealtime *)param;
    vTaskDelay(50);
    self->_wsClose();
    vTaskDelay(200);
    Serial.println("[WSS] DNS...");
    IPAddress ip;
    if (self->_dnsCached)
    {
        ip = self->_cachedIP;
        Serial.printf("[WSS] DNS缓存: %s\n", ip.toString().c_str());
    }
    else
    {
        if (!WiFi.hostByName("dashscope.aliyuncs.com", ip))
        {
            Serial.println("[WSS] DNS失败");
            self->_changeState(OMNI_ERROR);
            vTaskDelete(NULL);
            return;
        }
        self->_cachedIP = ip;
        self->_dnsCached = true;
        Serial.printf("[WSS] DNS: %s\n", ip.toString().c_str());
    }
    Serial.println("[WSS] TCP...");
    WiFiClient t;
    if (!t.connect(ip, 443, 5000))
    {
        Serial.println("[WSS] TCP失败");
        t.stop();
        self->_changeState(OMNI_ERROR);
        vTaskDelete(NULL);
        return;
    }
    t.stop();
    String wsPath = "/api-ws/v1/realtime?model=" + self->_model;
    Serial.println("[WSS] SSL+WS...");
    if (!self->_wsConnect("dashscope.aliyuncs.com", 443, wsPath.c_str()))
    {
        Serial.println("[WSS] 连接失败");
        self->_changeState(OMNI_ERROR);
        vTaskDelete(NULL);
        return;
    }
    Serial.println("[WSS] 连接成功");
    self->_sendSessionUpdate();
    Serial.println("[Omni] 等待响应...");
    unsigned long t0 = millis();
    bool got = false;
    while (millis() - t0 < 5000)
    {
        if (self->_ssl.available() > 0)
        {
            String m;
            if (self->_wsRecv(m))
            {
                self->_handleMessage(m.c_str());
                got = true;
                break;
            }
        }
        vTaskDelay(20);
    }
    if (!got)
    {
        Serial.println("[Omni] 5秒无响应");
        self->_changeState(OMNI_ERROR);
        vTaskDelete(NULL);
        return;
    }
    self->_reconnectDelay = 1000;
    self->_wsConnected = true;
    self->_lastPingMs = millis();
    Serial.println("[Omni] 连接完成");
    vTaskDelete(NULL);
}

// ===== 初始化 =====
void OmniRealtime::begin(const String &a, const String &v, const String &m, const String &i)
{
    _apiKey = a;
    _voice = v;
    _model = m;
    _instructions = i;
    _audioFramePrefix = "{\"type\":\"input_audio_buffer.append\",\"audio\":\"";
    _audioFrameSuffix = "\"}";
    if (!_ringBuf)
    {
        _ringBuf = (int16_t *)ps_malloc(OMNI_RING_BUF_SAMPLES * sizeof(int16_t));
        if (!_ringBuf)
            _ringBuf = (int16_t *)malloc(OMNI_RING_BUF_SAMPLES * sizeof(int16_t));
        if (_ringBuf)
        {
            memset(_ringBuf, 0, OMNI_RING_BUF_SAMPLES * sizeof(int16_t));
            Serial.printf("[Audio] 环形缓冲: %d字节\n", OMNI_RING_BUF_SAMPLES * (int)sizeof(int16_t));
        }
    }
    if (!_dataSem)
    {
        _dataSem = xSemaphoreCreateBinary();
        if (_dataSem)
            Serial.println("[Audio] 信号量就绪");
    }
    if (!_decodeBuf)
    {
        _decodeBuf = (uint8_t *)malloc(20480);
        if (_decodeBuf)
            Serial.println("[Audio] 解码缓冲就绪");
    }
    // AEC filter init
    for (int i = 0; i < 256; i++)
        _aecFilter[i] = 0;
}

void OmniRealtime::initPlaybackI2S(uint8_t bclk, uint8_t lrc, uint8_t dout)
{
    i2s_config_t cfg = {.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX), .sample_rate = OMNI_OUTPUT_SAMPLE_RATE, .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT, .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT, .communication_format = I2S_COMM_FORMAT_STAND_I2S, .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1, .dma_buf_count = 8, .dma_buf_len = 256, .use_apll = 0, .tx_desc_auto_clear = true, .fixed_mclk = I2S_PIN_NO_CHANGE};
    i2s_pin_config_t pin = {.bck_io_num = (int)bclk, .ws_io_num = (int)lrc, .data_out_num = (int)dout, .data_in_num = I2S_PIN_NO_CHANGE};
    i2s_driver_install(I2S_NUM_1, &cfg, 0, NULL);
    i2s_set_pin(I2S_NUM_1, &pin);
    _i2sInitialized = true;
}

void OmniRealtime::setVolume(uint8_t vol) { _playVolume = constrain(vol, 0, 100); }

bool OmniRealtime::connect()
{
    if (_state >= OMNI_CONNECTING && _state <= OMNI_SPEAKING)
        return true;
    _changeState(OMNI_CONNECTING);
    xTaskCreate(_connectTaskFunc, "wss_con", 8192, this, 2, NULL);
    _lastReconnectAttempt = millis();
    return true;
}

void OmniRealtime::disconnect()
{
    if (_playTask)
    {
        vTaskDelete(_playTask);
        _playTask = nullptr;
    }
    _wsClose();
    _wsConnected = false;
    _state = OMNI_DISCONNECTED;
}
bool OmniRealtime::isConnected() { return _state >= OMNI_SESSION_READY; }

// ===== AEC回声消除（暂禁用，排查二次识别问题） =====
void OmniRealtime::processAudio(int16_t *pcmData, size_t samples)
{
    if (!pcmData || samples == 0 || samples > 640)
        return;
    if (_playTask == nullptr)
        return;
    if (_aecWriteIdx < 640)
        return;

    size_t delayShift = 16;
    size_t refStart = (_aecWriteIdx > delayShift + samples) ? (_aecWriteIdx - delayShift - samples) : 0;
    refStart %= 640;

    long refPower = 0;
    for (size_t i = 0; i < samples; i++)
    {
        int16_t r = _aecBuf[(refStart + i) % 640];
        refPower += (long)r * r;
    }
    refPower /= samples;
    if (refPower < 10)
        return;

    float step = _aecGain;
    for (size_t i = 0; i < samples; i++)
    {
        size_t idx = (refStart + i) % 640;
        float ref = _aecBuf[idx];

        float echoEst = 0;
        int flen = (_aecFilterOrder < (int)samples) ? _aecFilterOrder : (int)samples;
        for (int j = 0; j < flen && (int)i >= j; j++)
        {
            size_t rj = (idx >= (size_t)j) ? idx - j : 640 + idx - j;
            echoEst += _aecFilter[j] * _aecBuf[rj];
        }

        float error = (float)pcmData[i] - echoEst;
        if (error > 32767.0f)
            error = 32767.0f;
        if (error < -32768.0f)
            error = -32768.0f;

        // 双讲检测：误差大时冻结系数，防止发散
        // 双讲：误差绝对值超过300就算人声（不设比例，小声也能触发）
        bool doubleTalk = (fabsf(error) > 200.0f);
        float out = doubleTalk ? (float)pcmData[i] : error;
        if (!doubleTalk)
        {
            float mu = step / (refPower + 1.0f);
            for (int j = 0; j < flen && (int)i >= j; j++)
            {
                size_t rj = (idx >= (size_t)j) ? idx - j : 640 + idx - j;
                _aecFilter[j] += mu * error * _aecBuf[rj];
            }
        }

        if (out > 32767.0f)
            out = 32767.0f;
        if (out < -32768.0f)
            out = -32768.0f;
        pcmData[i] = (int16_t)out;
    }

    float norm = 0;
    for (int i = 0; i < _aecFilterOrder; i++)
        norm += _aecFilter[i] * _aecFilter[i];
    if (norm > 1.0f)
    {
        float s = 1.0f / sqrtf(norm);
        for (int i = 0; i < _aecFilterOrder; i++)
            _aecFilter[i] *= s;
    }
}

// ===== 取消回复 =====
void OmniRealtime::cancelResponse()
{
    if (_state == OMNI_SPEAKING)
    {
        String msg = "{\"type\":\"response.cancel\"}";
        _wsSend(msg);
        Serial.println("[Omni] -> cancel");
        // 本地立刻停播放
        // 只清缓冲，不杀播放任务（防止锁泄漏导致后续无声音）
        portENTER_CRITICAL(&_bufLock);
        _ringWritePos = _ringReadPos = 0;
        portEXIT_CRITICAL(&_bufLock);
    }
}

// ===== 发送音频 =====
void OmniRealtime::sendAudio(const uint8_t *pcmData, size_t len)
{
    if (_state < OMNI_SESSION_READY || !_wsConnected)
        return;
    // 噪声门：播放时能量太低不发（防残留回声）
    if (_playTask != nullptr)
    {
        long e = 0;
        int n = len / 2;
        if (n > 128)
            n = 128;
        for (int i = 0; i < n; i++)
        {
            int s = ((int16_t *)pcmData)[i];
            if (s < 0)
                s = -s;
            e += s;
        }
        if (e < n * 15)
            return;
    }
    String b64 = base64::encode(pcmData, len);
    _wsSend(_audioFramePrefix + b64 + _audioFrameSuffix);
}

// ===== 主循环 =====
void OmniRealtime::loop()
{
    String msg;
    while (_wsConnected && _wsRecv(msg))
        _handleMessage(msg.c_str());

    if (_wsConnected && _ssl.connected())
    {
        unsigned long n = millis();
        if (n - _lastPingMs >= PING_INTERVAL)
        {
            if (_wsSendPing())
                _lastPingMs = n;
            else
            {
                _wsConnected = false;
                _changeState(OMNI_DISCONNECTED);
            }
        }
    }
    if (_wsConnected && !_ssl.connected())
    {
        _wsConnected = false;
        _changeState(OMNI_DISCONNECTED);
    }
    if ((_state == OMNI_DISCONNECTED || _state == OMNI_ERROR) && WiFi.status() == WL_CONNECTED)
    {
        unsigned long n = millis();
        if (n - _lastReconnectAttempt > _reconnectDelay)
        {
            Serial.printf("[Omni] 重连 (%ums)...\n", _reconnectDelay);
            connect();
            _reconnectDelay = (_reconnectDelay * 2 > RECONNECT_DELAY_MAX) ? RECONNECT_DELAY_MAX : _reconnectDelay * 2;
        }
    }
}

// ===== session.update（服务端VAD，快速触发 + 本地AEC辅助） =====
void OmniRealtime::_sendSessionUpdate()
{
    DynamicJsonDocument doc(1536);
    doc["event_id"] = "evt_" + String(millis());
    doc["type"] = "session.update";
    JsonObject s = doc.createNestedObject("session");
    s["model"] = _model;
    s["modalities"].add("text");
    s["modalities"].add("audio");
    s["voice"] = _voice;
    s["input_audio_format"] = "pcm";
    s["output_audio_format"] = "pcm";
    // 服务端VAD：参数激进但可靠，本地AEC已净化音频减少误触发
    JsonObject td = s.createNestedObject("turn_detection");
    td["type"] = "server_vad";
    td["threshold"] = 0.5;
    td["silence_duration_ms"] = 400;
    td["prefix_padding_ms"] = 50; // 50ms VAD确认，打断更快
    s["instructions"] = _instructions;
    String json;
    serializeJson(doc, json);
    if (!_wsSend(json))
    {
        Serial.println("[Omni] session.update发送失败");
        return;
    }
    vTaskDelay(100);
    if (_ssl.available() > 0)
    {
        String r;
        if (_wsRecv(r))
            _handleMessage(r.c_str());
    }
}

// ===== 消息处理 =====
void OmniRealtime::_handleMessage(const char *js)
{
    const char *tp = strstr(js, "\"type\":\"");
    if (!tp)
        return;
    tp += 8;
    const char *tq = strchr(tp, '"');
    if (!tq)
        return;
    String type;
    type.concat(tp, tq - tp);

    if (type == "response.audio.delta")
    {
        Serial.println("[Omni] << audio.delta");
        _changeState(OMNI_SPEAKING);
        const char *p = strstr(js, "\"delta\":\"");
        if (p)
        {
            p += 9;
            const char *q = strchr(p, '"');
            if (q)
            {
                size_t bl = q - p;
                char *b = (char *)malloc(bl + 1);
                if (b)
                {
                    memcpy(b, p, bl);
                    b[bl] = 0;
                    _processAudioDelta(b);
                    free(b);
                }
            }
        }
        return;
    }

    DynamicJsonDocument doc(4096);
    if (deserializeJson(doc, js))
        return;

    if (type == "session.created" || type == "session.updated")
    {
        Serial.printf("[Omni] 就绪 %s\n", _voice.c_str());
        _changeState(OMNI_SESSION_READY);
        _changeState(OMNI_LISTENING);
    }
    else if (type == "input_audio_buffer.speech_started")
    {
        _changeState(OMNI_USER_SPEAKING);
        _userTextBuffer = "";
        // 只清缓冲，不杀播放任务（防止锁泄漏导致后续无声音）
        portENTER_CRITICAL(&_bufLock);
        _ringWritePos = _ringReadPos = 0;
        portEXIT_CRITICAL(&_bufLock);
    }
    else if (type == "input_audio_buffer.speech_stopped")
    {
        _changeState(OMNI_PROCESSING);
    }
    else if (type == "conversation.item.input_audio_transcription.completed")
    {
        _userTextBuffer = doc["transcript"].as<String>();
        Serial.printf("[Omni] 识别: %s\n", _userTextBuffer.c_str());
    }
    else if (type == "response.audio_transcript.delta")
    {
        _assistantTextBuffer += doc["delta"].as<String>();
    }
    else if (type == "response.audio_transcript.done")
    {
        Serial.printf("[Omni] 输出: %s\n", (doc["text"] | _assistantTextBuffer).c_str());
    }
    else if (type == "response.audio.done" || type == "response.done")
    {
        if (!_playTask && _ringBuf)
        {
            portENTER_CRITICAL(&_bufLock);
            size_t a = (_ringWritePos >= _ringReadPos) ? _ringWritePos - _ringReadPos : OMNI_RING_BUF_SAMPLES - _ringReadPos + _ringWritePos;
            portEXIT_CRITICAL(&_bufLock);
            if (a > 0)
                _startPlayTask();
        }
        if (type == "response.done")
        {
            _assistantTextBuffer = "";
            _changeState(OMNI_LISTENING);
            if (_onWaitingForSpeech)
                _onWaitingForSpeech("{\"status\":\"listening\"}");
            Serial.println("[Omni] 回复结束");
        }
    }
    else if (type == "error")
    {
        Serial.printf("[Omni] ❌ %s\n", doc["message"].as<String>().c_str());
    }
}

// ===== PCM解码 → 环形缓冲 =====
void OmniRealtime::_processAudioDelta(const char *b64)
{
    if (!b64 || !_i2sInitialized || !_ringBuf || !_dataSem || !_decodeBuf)
        return;
    size_t ml = (strlen(b64) / 4) * 3;
    if (ml > 20480)
        ml = 20480;
    size_t len = base64::decode(b64, _decodeBuf, ml);
    int16_t *pcm = (int16_t *)_decodeBuf;
    size_t sc = len / 2;

    // 跟踪delta到达间隔，评估网络状态
    unsigned long now = millis();

    portENTER_CRITICAL(&_bufLock);
    for (size_t i = 0; i + 2 < sc; i += 3)
    {
        _ringBuf[_ringWritePos] = pcm[i];
        _ringWritePos = (_ringWritePos + 1) % OMNI_RING_BUF_SAMPLES;
        if (_ringWritePos == _ringReadPos)
            _ringReadPos = (_ringReadPos + 1) % OMNI_RING_BUF_SAMPLES;
        int16_t s = ((int32_t)pcm[i + 1] + pcm[i + 2]) >> 1;
        _ringBuf[_ringWritePos] = s;
        _ringWritePos = (_ringWritePos + 1) % OMNI_RING_BUF_SAMPLES;
        if (_ringWritePos == _ringReadPos)
            _ringReadPos = (_ringReadPos + 1) % OMNI_RING_BUF_SAMPLES;
    }
    portEXIT_CRITICAL(&_bufLock);
    xSemaphoreGive(_dataSem);
    if (!_playTask)
    {
        _startPlayTask();
    }
}

void OmniRealtime::onUserText(OmniTextCallback cb) { _onUserText = cb; }
void OmniRealtime::onAssistantText(OmniTextCallback cb) { _onAssistantText = cb; }
void OmniRealtime::onWaitingForSpeech(OmniTextCallback cb) { _onWaitingForSpeech = cb; }

void OmniRealtime::_startPlayTask()
{
    if (!_playTask)
        xTaskCreatePinnedToCore(_playTaskFunc, "play", 4096, this, 1, &_playTask, 1);
}

void OmniRealtime::_playTaskFunc(void *p)
{
    OmniRealtime *s = (OmniRealtime *)p;
    int16_t mono[128], stereo[256];
    while (s->_playTask)
    {
        xSemaphoreTake(s->_dataSem, portMAX_DELAY);
        portENTER_CRITICAL(&s->_bufLock);
        size_t wp = s->_ringWritePos, rp = s->_ringReadPos;
        size_t av = (wp >= rp) ? wp - rp : OMNI_RING_BUF_SAMPLES - rp + wp;
        size_t n = (av < 128) ? av : 128;
        for (size_t i = 0; i < n; i++)
        {
            int16_t v = s->_ringBuf[rp];
            rp = (rp + 1) % OMNI_RING_BUF_SAMPLES;
            mono[i] = v;
            s->_aecBuf[s->_aecWriteIdx % 640] = v;
            s->_aecWriteIdx++;
        }
        s->_ringReadPos = rp;
        if (av > n)
            xSemaphoreGive(s->_dataSem);
        portEXIT_CRITICAL(&s->_bufLock);
        if (n > 0)
        {
            int v = s->_playVolume;
            for (size_t i = 0; i < n; i++)
            {
                int16_t x = (int16_t)((mono[i] * v) / 100);
                stereo[i * 2] = stereo[i * 2 + 1] = x;
            }
            size_t w;
            i2s_write(I2S_NUM_1, stereo, n * 4, &w, portMAX_DELAY);
        }
    }
    vTaskDelete(NULL);
}
