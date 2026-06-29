
import asyncio
import json
import os
import shutil
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import toga
from toga.style import Pack


PACKAGE_DIR = Path(__file__).resolve().parent
HTTP_PORT = 8766
RELAY_PORT = 8765


def copy_if_missing(src: Path, dst: Path) -> None:
    if src.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def prepare_environment() -> tuple[Path, str]:
    base = Path.home() / "Documents" / "N.E.K.O_ESP32"
    data_dir = base / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    copy_if_missing(PACKAGE_DIR / "default_data/config.json", data_dir / "config.json")
    copy_if_missing(PACKAGE_DIR / "default_data/memory.json", data_dir / "memory.json")
    device_file = data_dir / "ios_device_id.json"
    if device_file.exists():
        try:
            device_id = json.loads(device_file.read_text(encoding="utf-8")).get("phone_id", "")
        except Exception:
            device_id = ""
    else:
        device_id = ""
    if not device_id:
        device_id = "PHONE-IOS-" + uuid.uuid4().hex[:16].upper()
        device_file.write_text(json.dumps({"phone_id": device_id}, ensure_ascii=False, indent=2), encoding="utf-8")

    os.environ["NEKO_ESP32_BASE_DIR"] = str(base)
    os.environ["NEKO_ESP32_PUBLIC_DIR"] = str(PACKAGE_DIR / "public")
    os.environ["NEKO_ESP32_DATA_DIR"] = str(data_dir)
    return base, device_id


def start_python_server() -> None:
    def runner():
        import server
        asyncio.run(server.run_async("0.0.0.0", HTTP_PORT, RELAY_PORT))

    threading.Thread(target=runner, name="neko-ios-python-server", daemon=True).start()


class NekoEsp32App(toga.App):
    def startup(self):
        _, phone_id = prepare_environment()
        start_python_server()
        url = f"http://127.0.0.1:{HTTP_PORT}/?phone_id={urllib.parse.quote(phone_id)}"
        self.main_window = toga.MainWindow(title=self.formal_name)
        try:
            self.webview = toga.WebView(url=url, style=Pack(flex=1))
        except TypeError:
            self.webview = toga.WebView(style=Pack(flex=1))
            self.webview.url = url
        self.main_window.content = self.webview
        self.main_window.show()
        # Give the local HTTP server a moment to bind, then force a load.
        threading.Thread(target=self._delayed_load, args=(url,), daemon=True).start()

    def _delayed_load(self, url: str) -> None:
        time.sleep(1.2)
        try:
            self.webview.url = url
        except Exception:
            pass


def main():
    return NekoEsp32App("N.E.K.O_ESP32", "com.neko.esp32ios")
