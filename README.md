# N.E.K.O_ESP32

This repository contains the N.E.K.O ESP32 assistant app-side code and ESP32 firmware-side code.

## Layout

- `app/server/` - Python relay server and browser configuration UI.
- `app/android/` - Android Python server app project.
- `app/ios/` - iOS Python server app project.
- `esp32/xiaozhi-esp32-main/` - ESP32 firmware project.

## Security Notes

Runtime files that may contain secrets or private conversation data are intentionally ignored:

- `app/server/data/config.json`
- `app/server/data/memory.json`
- `app/server/data/location.json`
- `app/server/data/uploads/`

Use `app/server/data/config.example.json` as the starting template for local configuration.

## Local Server

```bash
cd app/server
python3 -m pip install -r requirements.txt
python3 server.py --host 0.0.0.0 --port 8766 --relay-port 8765
```

Then open:

```text
http://电脑IP:8766
```
