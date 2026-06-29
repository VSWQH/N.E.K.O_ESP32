# N.E.K.O ESP32

N.E.K.O ESP32 是一个本地 Python 服务端 + ESP32 语音终端项目。服务端负责网页配置、语音模型中转、记忆、MCP 工具、天气和音乐逻辑；ESP32 固件通过 WebSocket 连接本地服务端。

## 仓库结构

```text
.
├── app-esp32/              # Python 服务端、网页配置页、记忆/MCP/音乐/天气逻辑
└── ESP32_AI_LLM-master/    # ESP32 PlatformIO 固件工程和相关资料
```

主要路径：

- APP / 服务端入口：`app-esp32/server.py`
- APP / 网页配置页：`app-esp32/public/index.html`
- ESP32 固件工程：`ESP32_AI_LLM-master/ESP32_AI_LLM-master/esp32AI_vscode/`
- ESP32 主程序：`ESP32_AI_LLM-master/ESP32_AI_LLM-master/esp32AI_vscode/src/main/main.cpp`
- ESP32 配置存储：`ESP32_AI_LLM-master/ESP32_AI_LLM-master/esp32AI_vscode/src/main/ConfigStorage.cpp`

## 开发环境

### APP / 服务端

- Python 3.10 或更新版本
- Python 依赖：见 `app-esp32/requirements.txt`
- 推荐在本机或局域网内运行，ESP32 通过电脑 IP 连接

### ESP32 固件

推荐使用 VS Code，不需要 Arduino IDE。

必须安装：

- Visual Studio Code
- VS Code 插件：PlatformIO IDE

建议安装：

- VS Code 插件：C/C++
- USB 串口驱动：CH340 或 CP210x，按开发板实际芯片选择

PlatformIO 会根据 `platformio.ini` 自动拉取 ESP32 平台和依赖库。当前主工程配置：

```ini
[env:esp32-s3-devkitm-1]
platform = espressif32 @ 6.5.0
board = esp32-s3-devkitm-1
framework = arduino
monitor_speed = 115200
```

## 服务端启动

```bash
cd app-esp32
python3 -m pip install -r requirements.txt
cp data/config.example.json data/config.json
cp data/memory.example.json data/memory.json
python3 server.py --host 0.0.0.0 --port 8766 --relay-port 8765
```

启动后打开配置页：

```text
http://电脑IP:8766
```

ESP32 WebSocket 中转地址：

```text
ws://电脑IP:8765/
```

`电脑IP` 要换成运行服务端设备在同一局域网内的 IP 地址。

## ESP32 编译和上传

用 VS Code 打开这个目录：

```text
ESP32_AI_LLM-master/ESP32_AI_LLM-master/esp32AI_vscode/
```

在 PlatformIO 插件中执行：

1. Build：编译固件
2. Upload：上传到 ESP32
3. Monitor：查看串口日志

也可以用命令行：

```bash
cd ESP32_AI_LLM-master/ESP32_AI_LLM-master/esp32AI_vscode
pio run
pio run -t upload
pio device monitor -b 115200
```

如果使用的不是 ESP32-S3 DevKitM-1，需要按实际开发板修改 `board`、分区表、PSRAM 和引脚配置。

## 配置和安全

以下文件属于本地运行时配置，不应提交到 GitHub：

- `app-esp32/data/config.json`
- `app-esp32/data/memory.json`
- `app-esp32/data/location.json`
- `app-esp32/data/uploads/`
- `ESP32_AI_LLM-master/**/config.json`
- `ESP32_AI_LLM-master/**/audio_logs/`

仓库只保留示例配置，例如：

- `app-esp32/data/config.example.json`
- `app-esp32/data/memory.example.json`
- `ESP32_AI_LLM-master/**/config.example.json`

不要把真实 DashScope API Key、Wi-Fi 密码、位置、记忆数据、音频上传文件、Android/iOS 签名文件提交到仓库。后续打包 Android 和 iOS 前，应继续做这些加固：

- API Key 不写死在客户端或固件里
- 局域网配置页增加访问控制
- 手机端本地密钥使用系统安全存储
- 打包产物、签名证书、`.apk`、`.ipa`、`.p12`、`.mobileprovision` 加入忽略列表

## 常见问题

### 配置页打不开

确认服务端正在运行，并且手机、电脑、ESP32 在同一个局域网。浏览器访问 `http://电脑IP:8766`，不要使用 `localhost` 给其他设备访问。

### ESP32 连不上服务端

确认固件中的 WebSocket 地址是 `ws://电脑IP:8765/`，并检查电脑防火墙是否允许局域网设备访问 8765 和 8766 端口。

### PlatformIO 下载依赖慢

首次打开工程会下载 ESP32 平台和库依赖，时间可能较长。保持 VS Code 右下角 PlatformIO 任务完成后再编译。

## 当前状态

本仓库用于保存 APP 端和 ESP32 端源码。当前已排除运行时密钥、记忆、位置、上传音频和构建产物；正式发布 Android/iOS 版本前仍建议继续进行安全加固和打包签名隔离。
