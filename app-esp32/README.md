# app-esp32

`app-esp32` 是 N.E.K.O ESP32 的本地 Python 服务端。它提供网页配置页、ESP32 WebSocket 中转、DashScope 实时语音连接，以及记忆、MCP 工具、天气和音乐相关逻辑。

## 开发环境

- Python 3.10 或更新版本
- 依赖文件：`requirements.txt`
- 推荐在本机或局域网内运行

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

## 启动

首次运行先创建本地配置文件：

```bash
cp data/config.example.json data/config.json
cp data/memory.example.json data/memory.json
```

启动服务：

```bash
python3 server.py --host 0.0.0.0 --port 8766 --relay-port 8765
```

浏览器配置页：

```text
http://电脑IP:8766
```

ESP32 WebSocket 地址：

```text
ws://电脑IP:8765/
```

`电脑IP` 是运行服务端设备在局域网内的 IP，不是 `localhost`。

## 重要文件

- `server.py`：服务端主入口
- `public/index.html`：网页配置和控制界面
- `requirements.txt`：Python 依赖
- `data/config.example.json`：配置模板
- `data/memory.example.json`：记忆数据模板
- `ios_runner.py`：iOS 打包/运行相关入口

## 本地配置

真实运行时文件不会提交到仓库，需要在本地从模板创建：

```text
data/config.json
data/memory.json
data/location.json
data/uploads/
```

这些文件可能包含 API Key、位置、记忆、音频上传内容等敏感数据，应只保存在本机。

## 安全注意

- 不要把真实 DashScope API Key 写入仓库
- 不要把记忆、位置、上传音频提交到 GitHub
- 手机端打包时不要把服务端密钥直接放进客户端代码
- 面向 Android/iOS 发布前，建议增加配置页鉴权、局域网访问限制和系统安全存储
