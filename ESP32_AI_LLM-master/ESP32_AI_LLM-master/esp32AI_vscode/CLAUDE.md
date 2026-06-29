# CLAUDE.md — ESP32 纯语音模块开发规范

## 方法论：按功能需求拆解 + 由小到大验证

每次遇到问题时：
1. **优先按功能需求拆解** — 先梳理功能链路的完整步骤，再逐个排查每个步骤是否正常
2. **由小到大验证** — 从最底层/最基础环节开始验证，确认一步后再往上层查

示例：说话没反应
- 首先列出功能链路：语音采集 → 发送到大模型 → 大模型处理 → 返回结果 → 播放
- 然后由小到大验证：
  - 步骤1：WSS 连上了吗？ → `[WSS] SSL已连接`
  - 步骤2：session.update 成功了吗？ → `[Omni] ✅ 会话就绪`
  - 步骤3：语音采集了吗？ → 检查 Audio1.Record 输出
  - 步骤4：音频帧发送了吗？ → 检查 `sendAudio` 日志
  - 步骤5：服务器返回文本了吗？ → `response.audio_transcript.delta`
  - 步骤6：服务器返回音频了吗？ → `response.audio.delta`
  - 步骤7：I2S 播放了吗？ → 检查环形缓冲写入
- 先查功能链路哪个环节断了，再查那个环节的代码问题

## 项目结构
```
esp32AI_vscode/src/main/
├── main.cpp              # 主程序入口
├── OmniRealtime.h/cpp    # Qwen3.5-Omni 实时语音（手写WSS）
├── BLEConfig.h/cpp       # BLE 配网服务 (NimBLE)
├── ConfigStorage.h/cpp   # NVS 配置存储
├── Audio1.h/cpp          # I2S 录音 (INMP441, 16kHz)
├── I2S.h/cpp             # I2S 驱动层
└── base64.h/cpp          # Base64 编解码

## 硬件IO（不改动）
ESP32-S3: I2S_DOUT=4, I2S_BCLK=5, I2S_LRC=6, PIN_I2S_BCLK=2, PIN_I2S_LRC=1, PIN_I2S_DIN=42
```
