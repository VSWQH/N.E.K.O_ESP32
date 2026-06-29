
import asyncio
import os
import shutil
import threading
from pathlib import Path

_started = False


def _copy_if_missing(src: Path, dst: Path) -> None:
    if src.exists() and not dst.exists():
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _prepare_env() -> None:
    package_dir = Path(__file__).resolve().parent
    home = Path(os.environ.get("HOME", str(package_dir / "home"))).resolve()
    data_dir = home / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    defaults = package_dir / "android_default_data"
    _copy_if_missing(defaults / "config.json", data_dir / "config.json")
    _copy_if_missing(defaults / "memory.json", data_dir / "memory.json")
    os.environ["NEKO_ESP32_BASE_DIR"] = str(home)
    os.environ["NEKO_ESP32_PUBLIC_DIR"] = str(package_dir / "public")
    os.environ["NEKO_ESP32_DATA_DIR"] = str(data_dir)


def start(host="0.0.0.0", http_port=8766, relay_port=8765):
    global _started
    if _started:
        return "already_started"
    _started = True
    _prepare_env()

    def run():
        import server
        asyncio.run(server.run_async(str(host), int(http_port), int(relay_port)))

    threading.Thread(target=run, name="neko-esp32-python", daemon=True).start()
    return "started"



def decode_audio_to_pcm(audio_path, pcm_path, sample_rate=16000):
    from java import jclass
    decoder = jclass("com.neko.esp32.pythonserver.AudioPcmDecoder")
    return decoder.decodeToPcm(str(audio_path), str(pcm_path), int(sample_rate))
