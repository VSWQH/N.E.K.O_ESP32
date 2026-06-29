#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import cgi
import html
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import traceback
import time
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

try:
    import websockets
except ImportError:
    websockets = None

PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("NEKO_ESP32_BASE_DIR", str(PACKAGE_DIR))).resolve()
PUBLIC_DIR = Path(os.environ.get("NEKO_ESP32_PUBLIC_DIR", str(BASE_DIR / "public"))).resolve()
DATA_DIR = Path(os.environ.get("NEKO_ESP32_DATA_DIR", str(BASE_DIR / "data"))).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
CONFIG_FILE = DATA_DIR / "config.json"
MEMORY_FILE = DATA_DIR / "memory.json"
LOCATION_FILE = DATA_DIR / "location.json"

DASHSCOPE_CLONE_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
DASHSCOPE_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_MODEL = "qwen3.5-omni-flash-realtime"
DEFAULT_RELAY_PORT = 8765
DEFAULT_HTTP_PORT = 8766
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
DEFAULT_WAKE_WORD = "悠怡"
DEFAULT_WAKE_TIMEOUT_SECONDS = 45
YUI_WAKE_ALIASES = (
    "林悠怡", "林优衣", "林由依", "林有仪", "林悠依", "林悠一",
    "林友谊", "林有意", "林有一", "林友一", "林优一", "林由一",
    "林游艺", "林优异", "林雨衣", "林羽衣", "林语义", "林寓意",
    "悠依", "悠伊", "悠仪", "悠一", "幽依", "幽怡", "幽一",
    "优衣", "优依", "优怡", "优仪", "优宜", "优一", "优异",
    "由依", "由怡", "由一", "有依", "有怡", "有仪", "有意", "有一", "有益", "有义",
    "友谊", "友依", "友怡", "友仪", "友意", "友一","友宜", "友义", "友益",
    "游艺", "游衣", "游依", "游怡", "游一","游仪", "游意",
    "尤伊", "尤依", "尤怡", "犹疑","犹一", "又依", "又怡", "又仪", "又意", "又一",
    "雨衣", "羽衣", "语义", "寓意", "余一", "于一", "与一", "又一",
    "悠悠", "小悠", "小怡", "小依", "小伊", "小优", "小由",
)
WAKE_WORD_ALIASES = {
    "悠怡": YUI_WAKE_ALIASES,
    "林悠怡": ("悠怡",) + YUI_WAKE_ALIASES,
}

DEFAULT_INSTRUCTIONS = (
    "你的名字叫林悠怡，是一只15岁的猫娘。你称呼用户为'人类'或'碳基生物'。"
    "你嘴上傲娇但内心温柔，自称'本喵'。你喜欢待在用户身边，讨厌被忽视。"
    "请用傲娇但可爱的语气回答，每次回复不超过20字，要简短自然。"
)
DEFAULT_VOICES = [
    {"id": "Tina", "name": "Tina", "description": "甜美女声"},
    {"id": "Ethan", "name": "Ethan", "description": "阳光男声"},
    {"id": "Lucas", "name": "Lucas", "description": "沉稳男声"},
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_key": "",
    "dashscope_api_key": "",
    "voice": "Tina",
    "model": DEFAULT_MODEL,
    "volume": 50,
    "instructions": DEFAULT_INSTRUCTIONS,
    "mcp_enabled": True,
    "memory_enabled": True,
    "interrupt_enabled": False,
    "wake_word_enabled": True,
    "wake_word": DEFAULT_WAKE_WORD,
    "wake_timeout_seconds": DEFAULT_WAKE_TIMEOUT_SECONDS,
    "default_weather_city": "平湖市",
    "pairing_token": "",
    "last_cloned_voice": "",
    "last_clone_file": "",
}

SERVER_MCP_TOOLS = [
    {"name": "server.music.play", "description": "播放本机 Music 资料库歌曲；本地没有时播放在线试听。"},
    {"name": "server.music.search", "description": "在线搜索歌曲并打开结果。"},
    {"name": "server.music.pause", "description": "暂停音乐。"},
    {"name": "server.music.resume", "description": "继续播放音乐。"},
    {"name": "server.weather.query", "description": "查询城市或当前位置天气。"},
    {"name": "server.memory.query", "description": "查询服务端记住的最近播放歌曲和对话。"},
]

relay_state_lock = threading.Lock()
relay_state: Dict[str, Any] = {
    "esp32_connected": False,
    "dashscope_connected": False,
    "client": "",
    "last_event": "idle",
    "last_error": "",
    "audio_frames_from_esp32": 0,
    "audio_bytes_to_esp32": 0,
    "esp32_buffer_ms": 0,
    "esp32_buffer_pct": 0,
    "esp32_underflows": 0,
    "esp32_overruns": 0,
    "esp32_buffer_primed": False,
    "esp32_rebuffers": 0,
    "audio_packet_seq": 0,
    "audio_header_enabled": True,
    "server_tools": [t["name"] for t in SERVER_MCP_TOOLS],
    "last_tool_result": "",
}
music_preview_lock = threading.Lock()
music_preview_process: Any = None
active_device_lock = threading.Lock()
active_device: Dict[str, Any] = {"loop": None, "esp32": None, "session_id": "", "settings": None}
MUSIC_PCM_SAMPLE_RATE = 16000
MUSIC_PCM_BYTES_PER_SECOND = MUSIC_PCM_SAMPLE_RATE * 2
AUDIO_PACKET_PAYLOAD_BYTES = 1280
MUSIC_STREAM_CHUNK_BYTES = AUDIO_PACKET_PAYLOAD_BYTES
AUDIO_HEADER_MAGIC = b"NAUD"
AUDIO_HEADER_SIZE = 16
AUDIO_TYPE_TTS = 1
AUDIO_TYPE_MUSIC = 2
LEGACY_PACKAGED_VOICE_IDS = {
    "qwen-omni-vc-YUI-voice-20260626102719419-cdca",
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def normalize_wake_text(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", (text or "").lower())


def wake_word_candidates(wake_word: str) -> list[str]:
    word = normalize_wake_text(wake_word or DEFAULT_WAKE_WORD)
    candidates = {word}
    for alias in WAKE_WORD_ALIASES.get(wake_word or DEFAULT_WAKE_WORD, ()):
        normalized = normalize_wake_text(alias)
        if normalized:
            candidates.add(normalized)
    return [item for item in candidates if item]


def text_has_wake_word(text: str, wake_word: str) -> bool:
    normalized = normalize_wake_text(text)
    return any(candidate in normalized for candidate in wake_word_candidates(wake_word))


def text_has_sleep_command(text: str) -> bool:
    normalized = normalize_wake_text(text)
    return any(word in normalized for word in ("休眠", "睡觉", "退下", "先别听", "不用听了"))


def merged_config(data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()
    if data:
        cfg.update(data)
    if cfg.get("dashscope_api_key") and not cfg.get("api_key"):
        cfg["api_key"] = cfg["dashscope_api_key"]
    if cfg.get("api_key") and not cfg.get("dashscope_api_key"):
        cfg["dashscope_api_key"] = cfg["api_key"]
    voice = str(cfg.get("voice") or "Tina").strip() or "Tina"
    if voice in LEGACY_PACKAGED_VOICE_IDS and str(cfg.get("last_cloned_voice") or "").strip() != voice:
        voice = "Tina"
    cfg["voice"] = voice
    for key in ("mcp_enabled", "memory_enabled", "interrupt_enabled", "wake_word_enabled"):
        cfg[key] = coerce_bool(cfg.get(key), True)
    cfg["wake_word"] = str(cfg.get("wake_word") or DEFAULT_WAKE_WORD).strip() or DEFAULT_WAKE_WORD
    try:
        cfg["wake_timeout_seconds"] = max(5, min(300, int(cfg.get("wake_timeout_seconds", DEFAULT_WAKE_TIMEOUT_SECONDS) or DEFAULT_WAKE_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        cfg["wake_timeout_seconds"] = DEFAULT_WAKE_TIMEOUT_SECONDS
    try:
        cfg["volume"] = max(0, min(100, int(cfg.get("volume", 50) or 50)))
    except (TypeError, ValueError):
        cfg["volume"] = 50
    return cfg


def load_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        return merged_config()
    try:
        return merged_config(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        return merged_config()


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    ensure_dirs()
    clean = merged_config(cfg)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    return clean


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    return "*" * len(key) if len(key) <= 8 else key[:4] + "..." + key[-4:]


def generate_pairing_token() -> str:
    return secrets.token_urlsafe(24)


def ensure_pairing_token(cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = merged_config(cfg or load_config())
    token = str(cfg.get("pairing_token") or "").strip()
    if len(token) < 16:
        cfg["pairing_token"] = generate_pairing_token()
        cfg = save_config(cfg)
    return cfg


def token_hint(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    return token[:6] + "..." + token[-6:] if len(token) > 16 else "*" * len(token)


def pairing_token_valid(token: str | None, cfg: Dict[str, Any] | None = None) -> bool:
    token = (token or "").strip()
    if not token:
        return False
    expected = str((cfg or ensure_pairing_token()).get("pairing_token") or "").strip()
    return bool(expected) and hmac.compare_digest(token, expected)


def is_loopback_address(value: str) -> bool:
    value = (value or "").strip()
    if value in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def split_host_port(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    return value.split(":", 1)[0]


def allowed_cors_origin(origin: str, host_header: str) -> str:
    origin = (origin or "").strip()
    if not origin:
        return ""
    parsed = urlparse(origin)
    origin_host = (parsed.hostname or "").lower()
    request_host = split_host_port(host_header).lower()
    if origin_host and (origin_host == request_host or is_loopback_address(origin_host)):
        return origin
    return ""


def extract_bearer_token(value: str) -> str:
    value = (value or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def extract_query_value(query: str, name: str) -> str:
    for part in (query or "").split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        if key == name:
            return value
    return ""


def extract_http_token(handler: BaseHTTPRequestHandler) -> str:
    parsed = urlparse(handler.path)
    token = extract_query_value(parsed.query, "token") or extract_query_value(parsed.query, "pairing_token")
    if token:
        return token
    token = handler.headers.get("X-Neko-Token") or handler.headers.get("X-Pairing-Token") or ""
    if token:
        return token.strip()
    return extract_bearer_token(handler.headers.get("Authorization") or "")


def websocket_request_path(ws: Any, path: str | None = None) -> str:
    request_path = path or ""
    if not request_path:
        request = getattr(ws, "request", None)
        request_path = getattr(request, "path", "") if request is not None else ""
    if not request_path:
        request_path = getattr(ws, "path", "") or ""
    return request_path or "/"


def websocket_request_headers(ws: Any) -> Any:
    headers = getattr(ws, "request_headers", None)
    if headers is None:
        request = getattr(ws, "request", None)
        headers = getattr(request, "headers", None) if request is not None else None
    return headers


def extract_websocket_token(ws: Any, path: str | None) -> str:
    parsed = urlparse(websocket_request_path(ws, path))
    token = extract_query_value(parsed.query, "token") or extract_query_value(parsed.query, "pairing_token")
    if token:
        return token
    headers = websocket_request_headers(ws)
    if headers is not None:
        try:
            return (headers.get("X-Neko-Token") or headers.get("X-Pairing-Token") or extract_bearer_token(headers.get("Authorization") or "") or "").strip()
        except Exception:
            return ""
    return ""


def websocket_debug_info(ws: Any, path: str | None = None) -> str:
    request_path = websocket_request_path(ws, path)
    headers = websocket_request_headers(ws)
    token = extract_websocket_token(ws, path)
    return f"ws={type(ws).__module__}.{type(ws).__name__} path={request_path} token={'yes' if token else 'no'} headers={'yes' if headers is not None else 'no'}"

def public_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    public = {k: v for k, v in cfg.items() if k not in ("api_key", "dashscope_api_key")}
    key = cfg.get("api_key") or cfg.get("dashscope_api_key") or ""
    token = str(cfg.get("pairing_token") or "").strip()
    public["api_key_set"] = bool(key)
    public["api_key_hint"] = mask_key(key)
    public["pairing_token_set"] = bool(token)
    public["pairing_token_hint"] = token_hint(token)
    if token:
        public["pairing_token"] = token
    return public



def runtime_settings_from_config(cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    clean = merged_config(cfg or load_config())
    return {
        "volume": int(clean.get("volume", 50) or 50),
        "mcp_enabled": coerce_bool(clean.get("mcp_enabled"), True),
        "memory_enabled": coerce_bool(clean.get("memory_enabled"), True),
        "interrupt_enabled": coerce_bool(clean.get("interrupt_enabled"), False),
        "wake_word_enabled": coerce_bool(clean.get("wake_word_enabled"), True),
        "wake_word": str(clean.get("wake_word") or DEFAULT_WAKE_WORD).strip() or DEFAULT_WAKE_WORD,
        "wake_timeout_seconds": int(clean.get("wake_timeout_seconds") or DEFAULT_WAKE_TIMEOUT_SECONDS),
    }


def runtime_config_payload(settings: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "config",
        "volume": max(0, min(100, int(settings.get("volume", 50) or 50))),
        "interrupt_enabled": coerce_bool(settings.get("interrupt_enabled"), False),
        "wake_word_enabled": coerce_bool(settings.get("wake_word_enabled"), True),
        "wake_word": str(settings.get("wake_word") or DEFAULT_WAKE_WORD).strip() or DEFAULT_WAKE_WORD,
        "wake_timeout_seconds": int(settings.get("wake_timeout_seconds") or DEFAULT_WAKE_TIMEOUT_SECONDS),
    }


def default_memory() -> Dict[str, Any]:
    return {
        "notes": [],
        "recent_turns": [],
        "music_history": [],
        "facts": {"master": [], "assistant": [], "relationship": []},
        "episodes": [],
    }


def normalize_fact_key(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", (text or "").lower())


def normalize_memory(memory: Dict[str, Any] | None) -> Dict[str, Any]:
    base = default_memory()
    if not isinstance(memory, dict):
        return base
    if isinstance(memory.get("notes"), list):
        base["notes"] = memory["notes"][-100:]
    if isinstance(memory.get("recent_turns"), list):
        base["recent_turns"] = memory["recent_turns"][-60:]
    if isinstance(memory.get("music_history"), list):
        base["music_history"] = memory["music_history"][-40:]
    if isinstance(memory.get("episodes"), list):
        base["episodes"] = memory["episodes"][-200:]
    facts = memory.get("facts")
    if isinstance(facts, dict):
        for entity in ("master", "assistant", "relationship"):
            values = facts.get(entity)
            if isinstance(values, list):
                base["facts"][entity] = values[-120:]
    return base


def load_memory() -> Dict[str, Any]:
    if not MEMORY_FILE.exists():
        return default_memory()
    try:
        return normalize_memory(json.loads(MEMORY_FILE.read_text(encoding="utf-8")))
    except Exception:
        return default_memory()


def save_memory(memory: Dict[str, Any]) -> Dict[str, Any]:
    ensure_dirs()
    clean = normalize_memory(memory)
    tmp = MEMORY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(MEMORY_FILE)
    try:
        os.chmod(MEMORY_FILE, 0o600)
    except OSError:
        pass
    return clean


def upsert_memory_fact(memory: Dict[str, Any], entity: str, text: str, source: str = "dialog", evidence: int = 1) -> None:
    text = (text or "").strip()
    if not text:
        return
    if entity not in ("master", "assistant", "relationship"):
        entity = "relationship"
    facts = memory.setdefault("facts", {}).setdefault(entity, [])
    key = normalize_fact_key(text)
    now = datetime.now().isoformat(timespec="seconds")
    for fact in facts:
        if normalize_fact_key(str(fact.get("text") or "")) == key:
            fact["score"] = int(fact.get("score") or 1) + evidence
            fact["last_seen_at"] = now
            mentions = fact.setdefault("recent_mentions", [])
            if isinstance(mentions, list):
                mentions.append(now)
                fact["recent_mentions"] = mentions[-6:]
            return
    facts.append({
        "id": uuid.uuid4().hex,
        "text": text[:240],
        "source": source,
        "score": max(1, int(evidence or 1)),
        "created_at": now,
        "last_seen_at": now,
        "recent_mentions": [now],
        "suppress": False,
        "protected": False,
    })


def clean_memory_value(value: str, limit: int = 80) -> str:
    value = re.sub(r"^(一下|一点|一个|一首|这个|那个|就是)", "", value or "")
    value = re.sub(r"(呢|啊|呀|吧|哦|吗|嘛)$", "", value)
    return value.strip(" ：:，。！？,.!? ")[:limit]


def extract_user_memory_facts(text: str) -> list[tuple[str, str, str]]:
    raw = (text or "").strip()
    if len(normalize_fact_key(raw)) < 4:
        return []
    facts: list[tuple[str, str, str]] = []

    explicit_patterns = (
        r"(?:记住|记一下|你要记得|帮我记住|以后记得)[:：]?(?P<value>[^。！？!?]{2,100})",
        r"(?:以后|之后)(?P<value>[^。！？!?]{2,100})",
    )
    for pattern in explicit_patterns:
        for match in re.finditer(pattern, raw):
            value = clean_memory_value(match.group("value"), 120)
            if value:
                facts.append(("master", f"用户要求记住：{value}", "explicit"))

    name_patterns = (
        r"(?:我叫|我的名字叫|我的名字是)(?P<value>[^，。！？,.!?]{1,24})",
        r"(?:叫我|以后叫我)(?P<value>[^，。！？,.!?]{1,24})",
    )
    for pattern in name_patterns:
        for match in re.finditer(pattern, raw):
            value = clean_memory_value(match.group("value"), 24)
            if value:
                facts.append(("master", f"用户称呼是：{value}", "profile"))

    profile_patterns = (
        (r"我(?:最)?喜欢(?P<value>[^，。！？,.!?]{1,60})", "用户喜欢：{}"),
        (r"我(?:很)?爱(?P<value>[^，。！？,.!?]{1,60})", "用户喜欢：{}"),
        (r"我不喜欢(?P<value>[^，。！？,.!?]{1,60})", "用户不喜欢：{}"),
        (r"我讨厌(?P<value>[^，。！？,.!?]{1,60})", "用户讨厌：{}"),
        (r"我住在(?P<value>[^，。！？,.!?]{1,60})", "用户住在：{}"),
        (r"我来自(?P<value>[^，。！？,.!?]{1,60})", "用户来自：{}"),
    )
    for pattern, template in profile_patterns:
        for match in re.finditer(pattern, raw):
            value = clean_memory_value(match.group("value"), 60)
            if value:
                facts.append(("master", template.format(value), "profile"))

    preference_patterns = (
        (r"(?:回答|回复)(?:要|得|尽量)(?P<value>[^，。！？,.!?]{2,60})", "用户偏好的回答方式：{}"),
        (r"(?:别|不要)(?P<value>[^，。！？,.!?]{2,60})", "用户不希望：{}"),
    )
    for pattern, template in preference_patterns:
        for match in re.finditer(pattern, raw):
            value = clean_memory_value(match.group("value"), 60)
            if value:
                facts.append(("relationship", template.format(value), "preference"))

    seen = set()
    unique: list[tuple[str, str, str]] = []
    for entity, fact, source in facts:
        key = (entity, normalize_fact_key(fact))
        if key not in seen:
            seen.add(key)
            unique.append((entity, fact, source))
    return unique[:6]


def add_memory_note(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("记忆内容为空")
    memory = load_memory()
    memory["notes"].append({"id": uuid.uuid4().hex, "text": text[:500], "created_at": datetime.now().isoformat(timespec="seconds")})
    upsert_memory_fact(memory, "relationship", text[:240], "manual", 2)
    return save_memory(memory)


def add_memory_turn(role: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    memory = load_memory()
    role = "assistant" if role == "assistant" else "user"
    now = datetime.now().isoformat(timespec="seconds")
    memory["recent_turns"].append({"id": uuid.uuid4().hex, "role": role, "text": text[:500], "created_at": now})
    memory["episodes"].append({"id": uuid.uuid4().hex, "type": "dialog", "role": role, "text": text[:1000], "created_at": now})
    if role == "user":
        for entity, fact, source in extract_user_memory_facts(text):
            upsert_memory_fact(memory, entity, fact, source, 1)
    save_memory(memory)


def add_music_history(title: str, query: str = "", user_text: str = "") -> None:
    title = (title or "").strip()
    if not title:
        return
    memory = load_memory()
    now = datetime.now().isoformat(timespec="seconds")
    memory["music_history"].append({
        "id": uuid.uuid4().hex,
        "title": title[:200],
        "query": (query or "").strip()[:200],
        "user_text": (user_text or "").strip()[:300],
        "created_at": now,
    })
    memory["episodes"].append({"id": uuid.uuid4().hex, "type": "music", "title": title[:200], "query": (query or "").strip()[:200], "created_at": now})
    upsert_memory_fact(memory, "relationship", f"最近播放过音乐：{title[:160]}", "music", 1)
    save_memory(memory)


def compact_memory_text(value: str, limit: int = 72) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def trim_context_budget(sections: list[str], budget_chars: int = 1400) -> str:
    output: list[str] = []
    used = 0
    for section in sections:
        section = section.strip()
        if not section:
            continue
        extra = len(section) + (2 if output else 0)
        if used + extra <= budget_chars:
            output.append(section)
            used += extra
            continue
        remaining = budget_chars - used - (2 if output else 0)
        if remaining >= 80:
            output.append(section[:remaining].rstrip() + "\n- …")
        break
    return "\n\n".join(output)


def music_history_context(max_items: int = 2) -> str:
    memory = load_memory()
    items = [i for i in memory.get("music_history", [])[-max_items:] if str(i.get("title", "")).strip()]
    if not items:
        return ""
    lines = []
    for item in items:
        title = compact_memory_text(str(item.get("title") or ""), 48)
        lines.append(f"- 最近播放：{title}")
    return "[音乐播放]\n" + "\n".join(lines)


def last_music_title() -> str:
    memory = load_memory()
    for item in reversed(memory.get("music_history", [])):
        title = str(item.get("title") or "").strip()
        if title:
            return title
    for turn in reversed(memory.get("recent_turns", [])):
        if turn.get("role") != "user":
            continue
        text = str(turn.get("text") or "").strip()
        if not text or is_music_memory_question(text):
            continue
        if any(word in text for word in MUSIC_PLAY_WORDS):
            query = clean_music_query(text)
            if query and not any(word in query for word in ("什么", "哪", "几首")):
                return query
    return ""


def memory_facts_context(max_per_entity: int = 4) -> str:
    memory = load_memory()
    facts = memory.get("facts", {}) if isinstance(memory.get("facts"), dict) else {}
    labels = {"master": "用户", "assistant": "悠怡", "relationship": "关系偏好"}
    sections = []
    for entity in ("master", "relationship", "assistant"):
        items = [i for i in facts.get(entity, []) if isinstance(i, dict) and str(i.get("text") or "").strip() and not i.get("suppress")]
        if not items:
            continue
        items.sort(key=lambda i: (int(i.get("score") or 1), str(i.get("last_seen_at") or i.get("created_at") or "")), reverse=True)
        lines = [f"- {compact_memory_text(str(i.get('text') or ''), 72)}" for i in items[:max_per_entity]]
        sections.append(f"[{labels[entity]}]\n" + "\n".join(lines))
    return "\n\n".join(sections)


def memory_context(max_notes: int = 3, max_turns: int = 4, budget_chars: int = 1400) -> str:
    memory = load_memory()
    lines = []
    facts = memory_facts_context()
    if facts:
        lines.append(facts)
    notes = [compact_memory_text(str(i.get("text", "")).strip(), 80) for i in memory.get("notes", [])[-max_notes:] if str(i.get("text", "")).strip()]
    if notes:
        lines.append("[手动记忆]\n" + "\n".join("- " + n for n in notes))
    music = music_history_context()
    if music:
        lines.append(music)
    turns = [
        f"{'用户' if i.get('role') == 'user' else '助手'}: {compact_memory_text(str(i.get('text', '')).strip(), 80)}"
        for i in memory.get("recent_turns", [])[-max_turns:]
        if str(i.get("text", "")).strip()
    ]
    if turns:
        lines.append("[最近对话]\n" + "\n".join("- " + t for t in turns))
    return trim_context_budget(lines, budget_chars)


def compose_session_instructions(base: str, cfg: Dict[str, Any]) -> str:
    text = (base or DEFAULT_INSTRUCTIONS).strip()
    if coerce_bool(cfg.get("wake_word_enabled"), True):
        text += f"\n\n服务端已启用唤醒词“{cfg.get('wake_word') or DEFAULT_WAKE_WORD}”。用户说出唤醒词后才进入对话；回答时不要重复唤醒词本身。"
    if coerce_bool(cfg.get("memory_enabled"), True):
        mem = memory_context()
        if mem:
            text += "\n\n以下是服务端保存的记忆，回答时自然参考，不要逐条复述：\n" + mem
    return text


def load_location() -> Dict[str, Any]:
    if not LOCATION_FILE.exists():
        return {}
    try:
        data = json.loads(LOCATION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_location(data: Dict[str, Any]) -> Dict[str, Any]:
    ensure_dirs()
    clean: Dict[str, Any] = {"updated_at": datetime.now().isoformat(timespec="seconds"), "source": "browser"}
    label = str(data.get("label") or data.get("city") or "").strip()
    if label:
        clean["label"] = label[:80]
    for src, dst in (("lat", "lat"), ("latitude", "lat"), ("lon", "lon"), ("longitude", "lon")):
        if src in data:
            try:
                clean[dst] = round(float(data[src]), 6)
            except Exception:
                pass
    LOCATION_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return clean


def public_location() -> Dict[str, Any]:
    loc = load_location()
    return {**loc, "set": "lat" in loc and "lon" in loc} if loc else {"set": False}


def ip_location() -> Dict[str, Any]:
    providers = [
        ("ipapi", "https://ipapi.co/json/", lambda d: {"city": d.get("city"), "region": d.get("region"), "country": d.get("country_name"), "lat": d.get("latitude"), "lon": d.get("longitude")}),
        ("ipwhois", "https://ipwho.is/", lambda d: {"city": d.get("city"), "region": d.get("region"), "country": d.get("country"), "lat": d.get("latitude"), "lon": d.get("longitude")} if d.get("success") is not False else {}),
    ]
    for source, url, parser in providers:
        try:
            req = Request(url, headers={"User-Agent": "N.E.K.O_ESP32/2.0"})
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            parsed = parser(data)
        except Exception:
            continue
        result = {"source": source}
        city = str(parsed.get("city") or "").strip()
        region = str(parsed.get("region") or "").strip()
        country = str(parsed.get("country") or "").strip()
        label = " ".join(p for p in (country, region, city) if p)
        if label:
            result["label"] = label
        if city:
            result["city"] = city
        try:
            result["lat"] = float(parsed.get("lat"))
            result["lon"] = float(parsed.get("lon"))
        except Exception:
            pass
        if "lat" in result and "lon" in result:
            return result
        if city:
            return result
    return {}


def post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str] | None = None, timeout: int = 8) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "data": json.loads(text) if text else {}}
    except HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.read().decode("utf-8", errors="replace")}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": str(exc)}


def clone_voice(api_key: str, audio_bytes: bytes, filename: str, preferred_name: str, target_model: str) -> Dict[str, Any]:
    mime = mimetypes.guess_type(filename)[0] or "audio/mpeg"
    payload = {
        "model": "qwen-voice-enrollment",
        "input": {
            "action": "create",
            "target_model": target_model or DEFAULT_MODEL,
            "preferred_name": preferred_name or "esp32_voice",
            "audio": {"data": f"data:{mime};base64,{base64.b64encode(audio_bytes).decode('ascii')}"},
        },
    }
    result = post_json(DASHSCOPE_CLONE_URL, payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=90)
    if result.get("ok"):
        voice = result.get("data", {}).get("output", {}).get("voice")
        if voice:
            result["voice"] = voice
    return result


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def server_info() -> Dict[str, Any]:
    ip = lan_ip()
    cfg = ensure_pairing_token()
    token = str(cfg.get("pairing_token") or "").strip()
    relay_url = f"ws://{ip}:{AppHandler.relay_port}/"
    return {
        "lan_ip": ip,
        "http_port": AppHandler.http_port,
        "relay_port": AppHandler.relay_port,
        "http_url": f"http://{ip}:{AppHandler.http_port}",
        "local_http_url": f"http://127.0.0.1:{AppHandler.http_port}",
        "relay_url": relay_url,
        "relay_url_with_token": f"{relay_url}?token={quote(token)}" if token else relay_url,
        "websockets_available": websockets is not None,
    }


def update_relay_state(**values: Any) -> None:
    with relay_state_lock:
        relay_state.update(values)


def register_active_device(loop: Any, esp32: Any, session_id: str, settings: Dict[str, Any] | None = None) -> None:
    with active_device_lock:
        active_device.update({"loop": loop, "esp32": esp32, "session_id": session_id, "settings": settings})


def clear_active_device(session_id: str) -> None:
    with active_device_lock:
        if active_device.get("session_id") == session_id:
            active_device.update({"loop": None, "esp32": None, "session_id": "", "settings": None})


def active_device_snapshot() -> Dict[str, Any]:
    with active_device_lock:
        return dict(active_device)


def relay_status() -> Dict[str, Any]:
    with relay_state_lock:
        return dict(relay_state)


def json_bytes(payload: Dict[str, Any], status: int = 200) -> Tuple[int, bytes]:
    return status, json.dumps(payload, ensure_ascii=False).encode("utf-8")


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    raw = handler.rfile.read(int(handler.headers.get("Content-Length", "0") or "0"))
    return json.loads(raw.decode("utf-8")) if raw else {}


def sanitize_filename(filename: str) -> str:
    safe = "".join(ch for ch in os.path.basename(filename or "voice.mp3") if ch.isalnum() or ch in (" ", ".", "_", "-")).strip(" .")
    return safe[:120] or "voice.mp3"


def mcp_tool_result(name: str, ok: bool, message: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    result = {"name": name, "ok": bool(ok), "message": message, "content": [{"type": "text", "text": message}], "isError": not bool(ok)}
    if data is not None:
        result["data"] = data
    return result


def run_osascript(lines: list[str], args: list[str] | None = None, timeout: int = 8) -> Dict[str, Any]:
    cmd = ["osascript"]
    for line in lines:
        cmd.extend(["-e", line])
    if args:
        cmd.extend(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:
        return {"ok": False, "message": f"Music 调用失败：{exc}"}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    return {"ok": proc.returncode == 0, "message": out if proc.returncode == 0 else (err or out or "Music 调用失败")}


def open_url(url: str) -> Dict[str, Any]:
    try:
        proc = subprocess.run(["open", url], capture_output=True, text=True, timeout=5, check=False)
        return {"ok": proc.returncode == 0, "message": "已打开搜索结果" if proc.returncode == 0 else (proc.stderr or proc.stdout or "打开失败").strip()}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def search_music_online(query: str, limit: int = 5) -> Dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"ok": False, "message": "请说出要搜索的歌曲或歌手", "results": []}
    url = f"https://itunes.apple.com/search?term={quote(query)}&media=music&entity=song&limit={limit}&country=CN"
    try:
        req = Request(url, headers={"User-Agent": "N.E.K.O_ESP32/2.0"})
        with urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "message": f"在线搜歌失败：{exc}", "results": []}
    results = []
    for item in payload.get("results", [])[:limit]:
        title = str(item.get("trackName") or "").strip()
        artist = str(item.get("artistName") or "").strip()
        if title or artist:
            results.append({"title": title, "artist": artist, "album": str(item.get("collectionName") or ""), "url": str(item.get("trackViewUrl") or ""), "preview_url": str(item.get("previewUrl") or "")})
    if not results:
        return {"ok": False, "message": f"没有搜到：{query}", "results": []}
    first = results[0]
    return {"ok": True, "message": f"搜到 {first['title']} - {first['artist']}", "results": results}


def stop_music_preview() -> None:
    global music_preview_process
    with music_preview_lock:
        proc = music_preview_process
        music_preview_process = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    fallback = "/opt/homebrew/bin/ffmpeg"
    if Path(fallback).exists():
        return fallback
    raise RuntimeError("未找到 ffmpeg，无法把在线音乐解码成 ESP32 PCM")


def mobile_decode_audio_to_pcm(audio_path: Path, pcm_path: Path) -> str:
    messages = []
    for module_name, label in (("android_runner", "Android"), ("ios_runner", "iOS")):
        try:
            module = __import__(module_name)
        except Exception as exc:
            messages.append(f"{label} 解码器不可用：{exc}")
            continue
        try:
            result = module.decode_audio_to_pcm(str(audio_path), str(pcm_path), MUSIC_PCM_SAMPLE_RATE)
        except Exception as exc:
            messages.append(f"{label} 解码失败：{exc}")
            continue
        if pcm_path.exists() and pcm_path.stat().st_size > 0:
            return f"{label} 解码：{result or 'ok'}"
        messages.append(f"{label} 解码没有生成 PCM 音频")
    return "；".join(messages)


def download_preview_file(preview_url: str, title: str) -> Path:
    preview_url = (preview_url or "").strip()
    if not preview_url:
        raise ValueError("在线结果没有试听音频")
    ensure_dirs()
    suffix = Path(urlparse(preview_url).path).suffix or ".m4a"
    preview_path = DATA_DIR / f"music-{uuid.uuid4().hex[:8]}{suffix}"
    req = Request(preview_url, headers={"User-Agent": "N.E.K.O_ESP32/2.0"})
    with urlopen(req, timeout=15) as resp:
        preview_path.write_bytes(resp.read())
    return preview_path


def decode_audio_to_pcm(audio_path: Path) -> Path:
    pcm_path = audio_path.with_suffix(".pcm")
    try:
        decoder = ffmpeg_bin()
    except RuntimeError as ffmpeg_exc:
        mobile_message = mobile_decode_audio_to_pcm(audio_path, pcm_path)
        if pcm_path.exists() and pcm_path.stat().st_size > 0:
            update_relay_state(last_event=f"mobile decoded music: {mobile_message}")
            return pcm_path
        raise RuntimeError(f"{ffmpeg_exc}；{mobile_message}")
    cmd = [
        decoder,
        "-y",
        "-v",
        "error",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        str(MUSIC_PCM_SAMPLE_RATE),
        "-f",
        "s16le",
        str(pcm_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg 解码失败").strip())
    if not pcm_path.exists() or pcm_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg 没有生成 PCM 音频")
    return pcm_path


def safe_music_temp_path(path_value: str) -> Path | None:
    try:
        path = Path(str(path_value or "")).resolve()
        data_dir = DATA_DIR.resolve()
        if path.parent != data_dir:
            return None
        if not path.name.startswith("music-"):
            return None
        if path.suffix.lower() not in (".pcm", ".m4a", ".mp3", ".wav", ".aac"):
            return None
        return path
    except Exception:
        return None


async def cleanup_music_stream_files_later(stream: Dict[str, Any], delay_seconds: int = 60) -> None:
    await asyncio.sleep(delay_seconds)
    paths = []
    for key in ("pcm_path", "audio_path"):
        safe = safe_music_temp_path(str(stream.get(key) or ""))
        if safe and safe not in paths:
            paths.append(safe)
    deleted = []
    for path in paths:
        try:
            if path.exists():
                path.unlink()
                deleted.append(path.name)
        except Exception as exc:
            update_relay_state(last_event=f"music temp cleanup failed: {path.name} {exc}")
    if deleted:
        update_relay_state(last_event=f"music temp cleaned: {', '.join(deleted)}")


def prepare_online_music_stream(query: str) -> Dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return mcp_tool_result("server.music.play", False, "请说出要播放的歌曲名")
    search = search_music_online(query, limit=1)
    results = search.get("results") or []
    if not results:
        return mcp_tool_result("server.music.play", False, search.get("message") or f"没有搜到：{query}", {"query": query})
    first = results[0]
    title = str(first.get("title") or query).strip()
    artist = str(first.get("artist") or "").strip()
    display = f"{title} - {artist}" if artist else title
    try:
        audio_path = download_preview_file(str(first.get("preview_url") or ""), display)
        pcm_path = decode_audio_to_pcm(audio_path)
    except Exception as exc:
        if first.get("url"):
            open_url(str(first["url"]))
        return mcp_tool_result(
            "server.music.play",
            False,
            f"找到{display}，但无法解码给 ESP32：{exc}",
            {"query": query, "result": first},
        )
    return mcp_tool_result(
        "server.music.play",
        True,
        f"已准备播放：{display}",
        {
            "query": query,
            "source": "online_stream",
            "result": first,
            "music_stream": {
                "title": display,
                "audio_path": str(audio_path),
                "pcm_path": str(pcm_path),
                "bytes": pcm_path.stat().st_size,
                "sample_rate": MUSIC_PCM_SAMPLE_RATE,
                "format": "pcm_s16le_mono",
            },
        },
    )


def tool_music_search(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip()
    search = search_music_online(query)
    if search.get("ok") and coerce_bool(args.get("open"), True) and search.get("results"):
        open_url(search["results"][0].get("url") or f"https://music.apple.com/search?term={quote(query)}")
    return mcp_tool_result("server.music.search", bool(search.get("ok")), search.get("message", "搜歌失败"), {"query": query, "results": search.get("results", [])[:5]})


def tool_music_play(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip()
    stop_music_preview()
    return prepare_online_music_stream(query)


def tool_music_pause(args: Dict[str, Any]) -> Dict[str, Any]:
    stop_music_preview()
    if shutil.which("osascript") is None:
        return mcp_tool_result("server.music.pause", True, "已请求 ESP32 暂停音乐")
    result = run_osascript(['tell application "Music"', "pause", 'return "已暂停音乐"', "end tell"])
    return mcp_tool_result("server.music.pause", result["ok"], result["message"])


def tool_music_resume(args: Dict[str, Any]) -> Dict[str, Any]:
    title = last_music_title()
    if title:
        return tool_music_play({"query": title})
    return mcp_tool_result("server.music.resume", False, "还没有最近播放记录")


def tool_memory_query(args: Dict[str, Any]) -> Dict[str, Any]:
    topic = str(args.get("topic") or "").strip()
    if topic == "music_history":
        title = last_music_title()
        if not title:
            return mcp_tool_result("server.memory.query", True, "我还没有记到播放过的歌")
        return mcp_tool_result("server.memory.query", True, f"上一首播放的是：{title}", {"music_history": load_memory().get("music_history", [])[-8:]})
    ctx = memory_context()
    return mcp_tool_result("server.memory.query", True, ctx or "我还没有记到相关内容")


OPEN_METEO_WEATHER_CODES = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "中等毛毛雨",
    55: "较强毛毛雨",
    56: "冻毛毛雨",
    57: "较强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "较强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "中等阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴强冰雹",
}


def open_meteo_weather_desc(code: Any, cloud_cover: Any = None) -> str:
    try:
        code_i = int(code)
    except (TypeError, ValueError):
        return "未知"
    desc = OPEN_METEO_WEATHER_CODES.get(code_i, "未知")
    if code_i == 0:
        return "晴"
    if code_i in (1, 2) and cloud_cover is not None:
        try:
            cloud = int(float(cloud_cover))
            if cloud <= 20:
                return "晴"
        except (TypeError, ValueError):
            pass
    return desc


def source_label(location_data: Dict[str, Any]) -> str:
    source = str(location_data.get("source") or "").strip()
    if source in ("browser", "browser_saved"):
        return "浏览器定位"
    if source in ("ipapi", "ip-api", "ipwhois", "ip"):
        return "IP定位"
    if source == "geocoding":
        return "城市定位"
    if source == "default_city":
        return "默认城市"
    return "定位"


KNOWN_WEATHER_CITIES: Dict[str, Tuple[float, float, str]] = {
    "平湖": (30.6758, 121.0151, "浙江省 嘉兴市 平湖市"),
    "平湖市": (30.6758, 121.0151, "浙江省 嘉兴市 平湖市"),
    "浙江平湖": (30.6758, 121.0151, "浙江省 嘉兴市 平湖市"),
    "浙江省平湖市": (30.6758, 121.0151, "浙江省 嘉兴市 平湖市"),
    "上海": (31.22222, 121.45806, "中国 上海市 上海"),
    "上海市": (31.22222, 121.45806, "中国 上海市 上海"),
    "北京": (39.9075, 116.39723, "中国 北京市 北京"),
    "北京市": (39.9075, 116.39723, "中国 北京市 北京"),
    "深圳": (22.54554, 114.0683, "中国 广东 深圳"),
    "深圳市": (22.54554, 114.0683, "中国 广东 深圳"),
    "杭州": (30.29365, 120.16142, "中国 浙江 杭州"),
    "杭州市": (30.29365, 120.16142, "中国 浙江 杭州"),
    "嘉兴": (30.7522, 120.75, "中国 浙江 嘉兴"),
    "嘉兴市": (30.7522, 120.75, "中国 浙江 嘉兴"),
}


def known_weather_city(city: str) -> Dict[str, Any]:
    key = re.sub(r"\s+", "", city or "")
    item = KNOWN_WEATHER_CITIES.get(key)
    if not item:
        return {}
    lat, lon, label = item
    return {"lat": lat, "lon": lon, "label": label, "city": city, "source": "geocoding"}


def geocode_city(city: str) -> Dict[str, Any]:
    city = (city or "").strip()
    if not city:
        return {}
    known = known_weather_city(city)
    if known:
        return known
    compact = re.sub(r"\\s+", "", city)
    queries = [city]
    if compact.endswith(("市", "区", "县")) and len(compact) > 2:
        queries.append(compact[:-1])
    for query in dict.fromkeys(q for q in queries if q):
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={quote(query)}&count=5&language=zh&format=json"
        req = Request(url, headers={"User-Agent": "N.E.K.O_ESP32/2.0"})
        try:
            with urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            continue
        results = [item for item in (payload.get("results") or []) if isinstance(item, dict)]
        if not results:
            continue
        item = next((i for i in results if str(i.get("country_code") or "").upper() == "CN"), results[0])
        try:
            lat = float(item.get("latitude"))
            lon = float(item.get("longitude"))
        except (TypeError, ValueError):
            continue
        label = " ".join(str(item.get(k) or "").strip() for k in ("country", "admin1", "name") if str(item.get(k) or "").strip())
        return {"lat": lat, "lon": lon, "label": label or city, "city": city, "source": "geocoding"}
    return {}


def open_meteo_current(lat: float, lon: float) -> Dict[str, Any]:
    current = ",".join((
        "temperature_2m",
        "relative_humidity_2m",
        "apparent_temperature",
        "is_day",
        "precipitation",
        "rain",
        "weather_code",
        "cloud_cover",
        "wind_speed_10m",
    ))
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat:.6f}&longitude={lon:.6f}&current={current}&timezone=auto"
    req = Request(url, headers={"User-Agent": "N.E.K.O_ESP32/2.0"})
    with urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    current_data = payload.get("current") or {}
    if not isinstance(current_data, dict) or not current_data:
        raise ValueError("Open-Meteo 没有返回实时天气")
    return current_data


def current_weather_target(args: Dict[str, Any]) -> Tuple[float | None, float | None, str, Dict[str, Any]]:
    city = str(args.get("city") or "").strip()
    generic_city = city in ("", "本地", "这里", "当前位置", "附近", "当前", "今天", "现在", "当地")
    use_current = coerce_bool(args.get("use_current_location"), False) or generic_city
    if args.get("lat") is not None and args.get("lon") is not None:
        try:
            lat = float(args["lat"])
            lon = float(args["lon"])
            label = str(args.get("label") or "当前位置").strip() or "当前位置"
            return lat, lon, label, {"lat": lat, "lon": lon, "source": "browser"}
        except Exception:
            pass
    if use_current:
        default_city = str(load_config().get("default_weather_city") or DEFAULT_CONFIG.get("default_weather_city") or "").strip()
        if default_city:
            geocoded = geocode_city(default_city)
            if geocoded:
                geocoded["source"] = "default_city"
                return geocoded["lat"], geocoded["lon"], geocoded["label"], geocoded
        loc = load_location()
        if "lat" in loc and "lon" in loc:
            lat = float(loc["lat"])
            lon = float(loc["lon"])
            label = str(loc.get("label") or "当前位置").strip() or "当前位置"
            return lat, lon, label, {**loc, "source": loc.get("source") or "browser_saved"}
        loc = ip_location()
        if "lat" in loc and "lon" in loc:
            lat = float(loc["lat"])
            lon = float(loc["lon"])
            label = str(loc.get("label") or loc.get("city") or "电脑当前位置").strip()
            return lat, lon, label, loc
        if loc.get("city"):
            geocoded = geocode_city(str(loc["city"]))
            if geocoded:
                return geocoded["lat"], geocoded["lon"], geocoded["label"], geocoded
        return None, None, "当前位置", {"source": "ip", "error": "location_unavailable"}
    geocoded = geocode_city(city)
    if geocoded:
        return geocoded["lat"], geocoded["lon"], geocoded["label"], geocoded
    return None, None, city, {"city": city, "source": "geocoding", "error": "city_not_found"}


def tool_weather_query(args: Dict[str, Any]) -> Dict[str, Any]:
    lat, lon, label, location_data = current_weather_target(args)
    if lat is None or lon is None:
        return mcp_tool_result(
            "server.weather.query",
            False,
            "没能确定天气位置，请在页面点“使用当前位置”或直接说城市名。",
            {"label": label, "location": location_data},
        )
    try:
        current = open_meteo_current(float(lat), float(lon))
    except Exception as exc:
        return mcp_tool_result(
            "server.weather.query",
            False,
            f"天气查询失败：{exc}",
            {"lat": lat, "lon": lon, "label": label, "location": location_data, "source": "open-meteo"},
        )
    desc = open_meteo_weather_desc(current.get("weather_code"), current.get("cloud_cover"))
    temp = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")
    cloud = current.get("cloud_cover")
    rain = current.get("rain") or current.get("precipitation") or 0
    prefix = source_label(location_data)
    extra = ""
    if location_data.get("source") in ("ipapi", "ip-api", "ipwhois", "ip"):
        extra = "，位置可能有偏差"
    try:
        temp_i = round(float(temp))
    except (TypeError, ValueError):
        temp_i = temp
    try:
        feels_i = round(float(feels))
    except (TypeError, ValueError):
        feels_i = feels
    advice = "挺舒服。"
    try:
        t = float(temp)
        rain_f = float(rain or 0)
        wind_f = float(wind or 0)
        if rain_f > 0:
            advice = "出门记得带伞。"
        elif t >= 30:
            advice = "出门少穿点啦。"
        elif t >= 24:
            advice = "要出门就少穿点啦。"
        elif t <= 8:
            advice = "出门多穿点。"
        elif t <= 16:
            advice = "外面有点凉。"
        elif wind_f >= 30:
            advice = "风有点大。"
    except (TypeError, ValueError):
        pass
    message = f"{label}今天{desc}{temp_i}度，{advice}{extra}"
    return mcp_tool_result("server.weather.query", True, message, {
        "source": "open-meteo",
        "lat": lat,
        "lon": lon,
        "label": label,
        "location": location_data,
        "temperature_c": temp,
        "feels_like_c": feels,
        "humidity": humidity,
        "cloud_cover": cloud,
        "rain_mm": rain,
        "wind_kmph": wind,
        "weather_code": current.get("weather_code"),
        "description": desc,
    })


SERVER_TOOL_HANDLERS = {
    "server.music.play": tool_music_play,
    "server.music.search": tool_music_search,
    "server.music.pause": tool_music_pause,
    "server.music.resume": tool_music_resume,
    "server.weather.query": tool_weather_query,
    "server.memory.query": tool_memory_query,
}


def call_server_tool(name: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    handler = SERVER_TOOL_HANDLERS.get((name or "").strip())
    if handler is None:
        return mcp_tool_result(name or "unknown", False, f"未知服务端工具：{name}")
    try:
        result = handler(args if isinstance(args, dict) else {})
    except Exception as exc:
        result = mcp_tool_result(name, False, f"工具执行失败：{exc}")
    update_relay_state(last_event=f"server tool {name}", last_tool_result=result.get("message", ""))
    return result


MUSIC_STOP_WORDS = ("暂停", "停一下", "停止", "别放", "不要放", "关掉", "关了", "停掉", "音乐停", "歌停")
MUSIC_RESUME_WORDS = ("继续", "恢复", "接着放", "接着播", "继续放", "继续播放")
MUSIC_SEARCH_WORDS = ("搜歌", "搜一下", "搜索一下", "搜索", "搜", "找歌", "找一下", "找找", "找", "查一下歌", "查歌", "有没有")
MUSIC_PLAY_WORDS = (
    "播放一下", "播放一首", "播放", "播一下", "播一首", "播",
    "放一下", "放一首", "放首", "放个", "放点", "放",
    "听一下", "听一首", "听首", "听",
    "来一首", "来首", "来点", "整一首", "整首", "整点",
)
MUSIC_CONTEXT_WORDS = ("音乐", "歌曲", "歌", "曲子", "歌单", "专辑", "歌手")
MUSIC_MEMORY_QUESTION_WORDS = (
    "什么歌", "哪首歌", "哪一首", "歌名", "上一首", "上首",
    "刚才", "之前", "上次", "前面", "刚刚", "记得",
)
WEATHER_INTENT_WORDS = (
    "天气", "气温", "温度", "多少度", "几度", "冷不冷", "热不热", "冷吗", "热吗",
    "下雨", "下不下雨", "会不会下雨", "要不要带伞", "带伞", "雨大不大",
    "风大不大", "风速", "湿度", "空气", "外面", "天气预报", "预报",
)


def strip_wake_and_fillers(text: str) -> str:
    cleaned = text or ""
    for word in wake_word_candidates(DEFAULT_WAKE_WORD) + wake_word_candidates("林悠怡"):
        if word:
            cleaned = cleaned.replace(word, "")
    cleaned = re.sub(r"(麻烦你|麻烦|帮我|给我|请你|请|可以|能不能|能否|我要|我想|想要|现在|马上|顺便|那个|就是)", "", cleaned)
    return cleaned.strip(" ，。！？,.!?")


def clean_music_query(text: str) -> str:
    query = strip_wake_and_fillers(text)
    for marker in sorted(MUSIC_SEARCH_WORDS + MUSIC_PLAY_WORDS, key=len, reverse=True):
        if marker in query:
            query = query.split(marker, 1)[1]
            break
    query = re.sub(r"(的歌|的歌曲|的音乐)$", "", query).strip(" ，。！？,.!?")
    query = re.sub(r"(音乐|歌曲|歌单|歌|曲子|专辑|吧|一下|一首|一曲|一个|一点|点儿|来点|随机|随便|的)$", "", query)
    query = re.sub(r"(给我|帮我|请|播放|放|听|来|整|首|个)", "", query)
    query = query.strip(" ，。！？,.!?")
    query = re.sub(r"^(一首|一个|一点|点)", "", query).strip(" ，。！？,.!?")
    query = re.sub(r"的$", "", query).strip(" ，。！？,.!?")
    return query


def is_music_memory_question(text: str) -> bool:
    normalized = text or ""
    has_music = any(word in normalized for word in MUSIC_CONTEXT_WORDS) or any(word in normalized for word in ("听了", "听过", "播放了", "放了", "播了", "放过", "播过"))
    has_question = any(word in normalized for word in MUSIC_MEMORY_QUESTION_WORDS)
    has_ask = any(word in normalized for word in ("什么", "哪", "记不记得", "知道", "告诉我"))
    return has_music and has_question and has_ask


def extract_weather_city(text: str) -> str:
    phrase = strip_wake_and_fillers(text)
    if any(word in phrase for word in ("本地", "这里", "当前位置", "附近", "当前", "当地", "这边", "我这边", "外面")):
        return ""
    phrase = re.sub(r"(今天|明天|后天|现在|当前|最近|待会儿|待会|一会儿|一会|今晚|早上|上午|中午|下午|晚上|周末)", "", phrase)
    phrase = re.sub(r"(查询|查一下|查查|看看|看一下|看下|问一下|问问)", "", phrase)
    phrase = re.sub(r"(天气预报|天气|气温|温度|多少度|几度|冷不冷|热不热|冷吗|热吗|下不下雨|会不会下雨|下雨|要不要带伞|带伞|雨大不大|风大不大|风速|湿度|空气|预报|怎么样|如何|咋样|吗|呢|呀|啊|的)", "", phrase)
    phrase = phrase.strip(" ，。！？,.!?")
    if not phrase:
        return ""
    # Keep only the likely place phrase at the end, e.g. "查一下深圳" -> "深圳".
    return phrase[-20:]


def detect_server_tool_call(text: str) -> Tuple[str, Dict[str, Any]] | None:
    normalized = (text or "").strip()
    if not normalized:
        return None
    compact = normalize_wake_text(normalized)

    if is_music_memory_question(normalized):
        return "server.memory.query", {"topic": "music_history"}

    has_music_context = any(w in normalized for w in MUSIC_CONTEXT_WORDS)
    has_stop = any(w in normalized for w in MUSIC_STOP_WORDS)
    has_resume = any(w in normalized for w in MUSIC_RESUME_WORDS)
    if has_stop and (has_music_context or "播放" in normalized or "播" in normalized):
        return "server.music.pause", {}
    if has_resume and (has_music_context or "播放" in normalized or "播" in normalized or "放" in normalized):
        return "server.music.resume", {}

    has_weather = any(w in normalized for w in WEATHER_INTENT_WORDS) or any(w in compact for w in ("duoshaodu", "weather"))
    if has_weather:
        city = extract_weather_city(normalized)
        use_current = not city or any(w in normalized for w in ("本地", "这里", "当前位置", "附近", "当前", "当地", "这边", "我这边", "外面"))
        return "server.weather.query", {"city": city, "use_current_location": use_current}

    has_search = any(w in normalized for w in MUSIC_SEARCH_WORDS)
    has_play = any(w in normalized for w in MUSIC_PLAY_WORDS)
    if has_search and (has_music_context or clean_music_query(normalized)):
        query = clean_music_query(normalized)
        if query:
            return "server.music.search", {"query": query, "open": True}
    if has_play:
        query = clean_music_query(normalized)
        if query or has_music_context or any(w in normalized for w in ("随便", "随机", "来点")):
            return "server.music.play", {"query": query}
    return None


def downsample_24k_to_16k_stream(data: bytes, state: Dict[str, int]) -> bytes:
    sample_count = len(data) // 2
    if sample_count <= 0:
        return b""
    samples = struct.unpack(f"<{sample_count}h", data[: sample_count * 2])
    phase = int(state.get("phase", 0))
    out = []
    for sample in samples:
        if phase != 2:
            out.append(sample)
        phase = (phase + 1) % 3
    state["phase"] = phase
    return struct.pack(f"<{len(out)}h", *out) if out else b""


def apply_pcm_volume(data: bytes, volume: int) -> bytes:
    volume = max(0, min(100, int(volume)))
    if volume == 100:
        return data
    count = len(data) // 2
    if count <= 0:
        return b""
    scale = volume / 100.0
    samples = struct.unpack(f"<{count}h", data[: count * 2])
    out = [max(-32768, min(32767, int(s * scale))) for s in samples]
    return struct.pack(f"<{len(out)}h", *out)


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    loaded = False
    try:
        import certifi  # type: ignore
        ctx.load_verify_locations(cafile=certifi.where())
        loaded = True
    except Exception:
        pass
    bundled_certs = [
        PACKAGE_DIR / "certifi" / "cacert.pem",
        BASE_DIR / "certifi" / "cacert.pem",
    ]
    for cafile in bundled_certs:
        if cafile.exists():
            try:
                ctx.load_verify_locations(cafile=str(cafile))
                loaded = True
                break
            except Exception:
                pass
    android_ca_dir = Path("/system/etc/security/cacerts")
    if android_ca_dir.is_dir():
        try:
            ctx.load_verify_locations(capath=str(android_ca_dir))
            loaded = True
        except Exception:
            pass
    if not loaded:
        print("[TLS] using Python default CA store")
    return ctx


async def dashscope_connect(url: str, headers: Dict[str, str]):
    assert websockets is not None
    try:
        return await websockets.connect(url, additional_headers=headers, ssl=ssl_ctx(), ping_interval=20, ping_timeout=60, close_timeout=5, max_size=None)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers, ssl=ssl_ctx(), ping_interval=20, ping_timeout=60, close_timeout=5, max_size=None)


async def send_json_ws(ws, payload: Dict[str, Any]) -> None:
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def send_device_payload(esp32: Any, session_id: str, payload: Dict[str, Any]) -> None:
    payload.setdefault("session_id", session_id)
    await send_json_ws(esp32, payload)


def make_audio_packet(audio_type: int, seq: int, pcm: bytes) -> bytes:
    timestamp_ms = int(time.monotonic() * 1000) & 0xFFFFFFFF
    header = struct.pack("<4sBBHII", AUDIO_HEADER_MAGIC, 1, audio_type, AUDIO_HEADER_SIZE, seq & 0xFFFFFFFF, timestamp_ms)
    return header + pcm

def music_pace_delay(payload_len: int, buffer_ms: int) -> float:
    duration = payload_len / MUSIC_PCM_BYTES_PER_SECOND if payload_len > 0 else 0.0
    if buffer_ms <= 250:
        return max(0.012, duration * 0.30)
    if buffer_ms <= 700:
        return max(0.024, duration * 0.55)
    if buffer_ms <= 1400:
        return max(0.045, duration * 0.85)
    if buffer_ms <= 2400:
        return max(0.060, duration * 1.00)
    if buffer_ms <= 3200:
        return max(0.075, duration * 1.18)
    return max(0.100, duration * 1.55)


def tts_pace_delay(payload_len: int, buffer_ms: int) -> float:
    duration = payload_len / MUSIC_PCM_BYTES_PER_SECOND if payload_len > 0 else 0.0
    if buffer_ms >= 3200:
        return min(0.120, max(0.040, duration * 0.45))
    if buffer_ms >= 2400:
        return min(0.080, max(0.025, duration * 0.28))
    if buffer_ms >= 1600:
        return min(0.040, max(0.010, duration * 0.12))
    return 0.0


async def stream_music_to_esp32_socket(esp32: Any, session_id: str, stream: Dict[str, Any], allow_record: bool = False, send_audio: Any = None) -> None:
    pcm_path = Path(str(stream.get("pcm_path") or ""))
    title = str(stream.get("title") or "音乐")
    if not pcm_path.exists():
        await send_device_payload(esp32, session_id, {"type": "error", "message": f"音乐文件不存在：{title}"})
        return
    sent = 0
    stream_seq = int(relay_status().get("audio_packet_seq") or 0)
    try:
        await send_device_payload(esp32, session_id, {"relay_type": "control", "command": "clear_audio"})
        await asyncio.sleep(0.18)
        await send_device_payload(esp32, session_id, {"type": "status", "state": "music_start", "title": title, "wake_interrupt": allow_record})
        await send_device_payload(esp32, session_id, {"relay_type": "control", "command": "start_record" if allow_record else "stop_record"})
        await asyncio.sleep(0.05)
        with pcm_path.open("rb") as fh:
            while True:
                chunk = fh.read(MUSIC_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                if send_audio is not None:
                    await send_audio(AUDIO_TYPE_MUSIC, chunk, "music")
                else:
                    stream_seq = (stream_seq + 1) & 0xFFFFFFFF
                    await esp32.send(make_audio_packet(AUDIO_TYPE_MUSIC, stream_seq, chunk))
                    update_relay_state(audio_packet_seq=stream_seq, audio_header_enabled=True)
                sent += len(chunk)
                relay = relay_status()
                buffer_ms = int(relay.get("esp32_buffer_ms") or 0)
                update_relay_state(audio_bytes_to_esp32=sent, last_event=f"streaming music {title} {sent} bytes")
                if send_audio is None:
                    await asyncio.sleep(music_pace_delay(len(chunk), buffer_ms))
        await asyncio.sleep(0.12)
        await send_device_payload(esp32, session_id, {"type": "status", "state": "music_stop", "title": title, "bytes": sent})
        asyncio.create_task(cleanup_music_stream_files_later(stream, 60))
    except asyncio.CancelledError:
        await send_device_payload(esp32, session_id, {"type": "status", "state": "music_cancelled", "title": title, "bytes": sent})
        await send_device_payload(esp32, session_id, {"relay_type": "control", "command": "clear_audio"})
        raise
    except Exception as exc:
        await send_device_payload(esp32, session_id, {"type": "error", "message": f"音乐推送失败：{exc}"})
    finally:
        await send_device_payload(esp32, session_id, {"relay_type": "control", "command": "start_record"})


def schedule_active_esp32(coro: Any) -> Tuple[bool, str]:
    snap = active_device_snapshot()
    loop = snap.get("loop")
    esp32 = snap.get("esp32")
    if loop is None or esp32 is None or not snap.get("session_id"):
        return False, "ESP32 未连接"
    try:
        asyncio.run_coroutine_threadsafe(coro(esp32, str(snap["session_id"])), loop)
    except Exception as exc:
        return False, f"发送到 ESP32 失败：{exc}"
    return True, "已发送到 ESP32"



def sync_active_runtime_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    settings = runtime_settings_from_config(cfg)
    snap = active_device_snapshot()
    existing = snap.get("settings")
    if isinstance(existing, dict):
        existing.clear()
        existing.update(settings)

    async def push(esp32: Any, session_id: str) -> None:
        await send_device_payload(esp32, session_id, runtime_config_payload(settings))
        await send_device_payload(esp32, session_id, {
            "type": "hello",
            "transport": "websocket",
            "version": 1,
            "features": {
                "mcp": coerce_bool(settings.get("mcp_enabled"), True),
                "interrupt": coerce_bool(settings.get("interrupt_enabled"), False),
                "wake_word": coerce_bool(settings.get("wake_word_enabled"), True),
            },
            "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1, "frame_duration": 40},
            "server_audio_packet": {"magic": "NAUD", "version": 1, "header_size": AUDIO_HEADER_SIZE},
        })

    ok, message = schedule_active_esp32(push)
    if not ok and message == "ESP32 未连接":
        relay = relay_status()
        last_error = str(relay.get("last_error") or "").strip()
        last_event = str(relay.get("last_event") or "").strip()
        if last_error:
            message = f"ESP32 未保持连接，配置已保存；最近错误：{last_error}"
        elif last_event and last_event not in ("idle", "esp32 disconnected"):
            message = f"ESP32 未保持连接，配置已保存；最近状态：{last_event}"
        else:
            message = "ESP32 未连接，配置已保存；设备连上后会自动同步"
    return {"ok": ok, "message": message, "settings": runtime_config_payload(settings)}


def apply_tool_result_to_active_esp32(name: str, result: Dict[str, Any], args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = args if isinstance(args, dict) else {}
    message = str(result.get("message") or "")
    ok = bool(result.get("ok"))
    if name == "server.music.play" and ok:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        stream = data.get("music_stream") if isinstance(data, dict) else None
        if isinstance(stream, dict):
            title = str(stream.get("title") or "音乐").strip()
            if title:
                add_music_history(title, str(args.get("query") or ""), "UI MCP")
            sent, sent_message = schedule_active_esp32(lambda esp32, session_id: stream_music_to_esp32_socket(esp32, session_id, stream))
            return {"ok": sent, "message": sent_message, "action": "music_stream"}
    if name == "server.music.pause":
        async def pause(esp32: Any, session_id: str) -> None:
            await send_device_payload(esp32, session_id, {"type": "status", "state": "music_pause"})
            await send_device_payload(esp32, session_id, {"relay_type": "control", "command": "clear_audio"})
            await send_device_payload(esp32, session_id, {"relay_type": "control", "command": "start_record"})
        sent, sent_message = schedule_active_esp32(pause)
        return {"ok": sent, "message": sent_message, "action": "music_pause"}
    async def notify(esp32: Any, session_id: str) -> None:
        await send_device_payload(esp32, session_id, {"type": "mcp", "name": name, "ok": ok, "message": message, "data": result.get("data")})
    sent, sent_message = schedule_active_esp32(notify)
    return {"ok": sent, "message": sent_message, "action": "notify"}


async def relay_session(esp32, path: str | None = None) -> None:
    if websockets is None:
        await esp32.close(code=1011, reason="python websockets package missing")
        return
    cfg = ensure_pairing_token(load_config())
    ws_token = extract_websocket_token(esp32, path)
    update_relay_state(last_event=f"relay accepted {websocket_debug_info(esp32, path)}", last_error="")
    if not pairing_token_valid(ws_token, cfg):
        update_relay_state(last_error=f"invalid ESP32 pairing token; {websocket_debug_info(esp32, path)}", last_event="relay rejected")
        try:
            await send_json_ws(esp32, {"type": "error", "message": "invalid pairing token"})
            await asyncio.sleep(0.3)
        except Exception:
            pass
        await esp32.close(code=1008, reason="invalid pairing token")
        return
    api_key = (cfg.get("api_key") or cfg.get("dashscope_api_key") or "").strip()
    session_id = uuid.uuid4().hex
    model = cfg.get("model") or DEFAULT_MODEL
    voice = cfg.get("voice") or "Tina"
    instructions = compose_session_instructions(cfg.get("instructions") or DEFAULT_INSTRUCTIONS, cfg)
    session_settings = runtime_settings_from_config(cfg)
    register_active_device(asyncio.get_running_loop(), esp32, session_id, session_settings)
    update_relay_state(esp32_connected=True, dashscope_connected=False, client=str(getattr(esp32, "remote_address", "")), last_event="esp32 connected", last_error="", audio_frames_from_esp32=0, audio_bytes_to_esp32=0)
    print(f"[ESP32] connected {getattr(esp32, 'remote_address', '')} {websocket_debug_info(esp32, path)}")

    def mcp_enabled() -> bool:
        return coerce_bool(session_settings.get("mcp_enabled"), True)

    def memory_enabled() -> bool:
        return coerce_bool(session_settings.get("memory_enabled"), True)

    def interrupt_enabled() -> bool:
        return coerce_bool(session_settings.get("interrupt_enabled"), False)

    def wake_word_enabled() -> bool:
        return coerce_bool(session_settings.get("wake_word_enabled"), True)

    def wake_word_value() -> str:
        return str(session_settings.get("wake_word") or DEFAULT_WAKE_WORD).strip() or DEFAULT_WAKE_WORD

    def wake_timeout_value() -> int:
        return int(session_settings.get("wake_timeout_seconds") or DEFAULT_WAKE_TIMEOUT_SECONDS)

    dashscope = None
    listening = True
    tts_started = False
    tts_playback_started_at = 0.0
    downsample_state = {"phase": 0}
    wake_active_until = 0.0
    current_turn_allowed = not wake_word_enabled()
    wake_response_cancelled = False
    pending_music_stream: Dict[str, Any] | None = None
    music_task: asyncio.Task | None = None
    suppress_next_done_music = False
    audio_seq = 0
    esp32_buffer_ms = 0

    async def send_device(payload: Dict[str, Any]) -> None:
        payload.setdefault("session_id", session_id)
        await send_json_ws(esp32, payload)

    async def send_audio_frame(audio_type: int, pcm: bytes, pace_kind: str = "tts") -> None:
        nonlocal audio_seq
        if not pcm:
            return
        for offset in range(0, len(pcm), AUDIO_PACKET_PAYLOAD_BYTES):
            chunk = pcm[offset: offset + AUDIO_PACKET_PAYLOAD_BYTES]
            if not chunk:
                continue
            audio_seq = (audio_seq + 1) & 0xFFFFFFFF
            await esp32.send(make_audio_packet(audio_type, audio_seq, chunk))
            update_relay_state(audio_packet_seq=audio_seq, audio_header_enabled=True)
            buffer_ms = int(relay_status().get("esp32_buffer_ms") or 0)
            if pace_kind == "music":
                await asyncio.sleep(music_pace_delay(len(chunk), buffer_ms))
            else:
                delay = tts_pace_delay(len(chunk), buffer_ms)
                if delay > 0:
                    await asyncio.sleep(delay)


    async def send_runtime_to_device() -> None:
        await send_device(runtime_config_payload(session_settings))
        await send_device({
            "type": "hello",
            "transport": "websocket",
            "version": 1,
            "features": {
                "mcp": mcp_enabled(),
                "interrupt": interrupt_enabled(),
                "wake_word": wake_word_enabled(),
            },
            "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1, "frame_duration": 40},
            "server_audio_packet": {"magic": "NAUD", "version": 1, "header_size": AUDIO_HEADER_SIZE},
        })

    async def wait_for_required_api_key() -> str:
        nonlocal cfg, model, voice, instructions, session_settings
        warned = False
        while True:
            cfg = ensure_pairing_token(load_config())
            key = (cfg.get("api_key") or cfg.get("dashscope_api_key") or "").strip()
            model = cfg.get("model") or DEFAULT_MODEL
            voice = cfg.get("voice") or "Tina"
            instructions = compose_session_instructions(cfg.get("instructions") or DEFAULT_INSTRUCTIONS, cfg)
            session_settings = runtime_settings_from_config(cfg)
            register_active_device(asyncio.get_running_loop(), esp32, session_id, session_settings)
            if key:
                if warned:
                    update_relay_state(last_error="", last_event="api key ready")
                    await send_runtime_to_device()
                return key
            update_relay_state(last_error="DashScope API Key is empty", last_event="waiting api key")
            if not warned:
                warned = True
                await send_device({"type": "error", "message": "DashScope API Key is empty; please save it in the APP first"})
                await send_device({"type": "status", "state": "waiting_api_key", "message": "请先在 APP 保存 DashScope API Key"})
            try:
                msg = await asyncio.wait_for(esp32.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if isinstance(msg, bytes):
                continue
            try:
                data = json.loads(msg)
            except Exception:
                continue
            msg_type = data.get("type") or data.get("relay_type") or ""
            if msg_type == "hello":
                await send_runtime_to_device()

    def wake_is_active() -> bool:
        return (not wake_word_enabled()) or time.monotonic() < wake_active_until

    async def mark_awake(reason: str) -> None:
        nonlocal wake_active_until, current_turn_allowed
        wake_active_until = time.monotonic() + wake_timeout_value()
        current_turn_allowed = True
        await send_device({"type": "status", "state": "awake", "wake_word": wake_word_value(), "remaining_seconds": int(wake_timeout_value())})
        update_relay_state(last_event=f"wake active {reason}")

    async def cancel_sleeping_response(reason: str) -> None:
        nonlocal tts_started, listening, wake_response_cancelled
        wake_response_cancelled = True
        if dashscope is not None:
            try:
                await send_json_ws(dashscope, {"type": "response.cancel"})
            except Exception:
                pass
        downsample_state["phase"] = 0
        if tts_started:
            tts_started = False
            await send_device({"type": "tts", "state": "stop"})
        listening = True
        await send_device({"type": "status", "state": "sleeping", "wake_word": wake_word_value()})
        await send_device({"type": "control", "command": "clear_audio"})
        await send_device({"relay_type": "control", "command": "clear_audio"})
        await send_device({"relay_type": "control", "command": "start_record"})

    async def stop_current_response(reason: str) -> None:
        nonlocal tts_started, listening
        if dashscope is not None:
            try:
                await send_json_ws(dashscope, {"type": "response.cancel"})
            except Exception:
                pass
        downsample_state["phase"] = 0
        tts_started = False
        listening = True
        await send_device({"type": "abort", "reason": reason})
        await send_device({"type": "tts", "state": "stop"})
        await send_device({"type": "control", "command": "clear_audio"})
        await send_device({"relay_type": "control", "command": "clear_audio"})
        await send_device({"relay_type": "control", "command": "start_record"})

    async def stream_music_to_esp32(stream: Dict[str, Any]) -> None:
        nonlocal listening
        listening = bool(wake_word_enabled())
        try:
            await stream_music_to_esp32_socket(esp32, session_id, stream, allow_record=bool(wake_word_enabled()), send_audio=send_audio_frame)
        finally:
            listening = True

    async def stop_music_stream(reason: str) -> None:
        nonlocal music_task, pending_music_stream, listening
        pending_music_stream = None
        task = music_task
        active = task is not None and not task.done()
        music_task = None
        if active:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        listening = True
        if active or reason != "new_music":
            await send_device({"type": "status", "state": "music_interrupted", "reason": reason})
            await send_device({"relay_type": "control", "command": "clear_audio"})
            await send_device({"relay_type": "control", "command": "start_record"})
            update_relay_state(last_event=f"music interrupted {reason}")

    async def start_music_stream_task(stream: Dict[str, Any]) -> None:
        nonlocal music_task
        await stop_music_stream("new_music")
        async def runner() -> None:
            nonlocal music_task
            try:
                await stream_music_to_esp32(stream)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                update_relay_state(last_error=f"music stream failed: {exc}", last_event="music stream failed")
                try:
                    await send_device({"type": "error", "message": f"音乐推送失败：{exc}"})
                except Exception:
                    pass
            finally:
                if music_task is asyncio.current_task():
                    music_task = None
        music_task = asyncio.create_task(runner())

    async def run_server_tool_for_user(name: str, args: Dict[str, Any], user_text: str) -> None:
        nonlocal pending_music_stream, suppress_next_done_music, tts_started, listening
        suppress_next_done_music = True
        if dashscope is not None:
            try:
                await send_json_ws(dashscope, {"type": "response.cancel"})
            except Exception:
                pass
        if tts_started:
            tts_started = False
            downsample_state["phase"] = 0
            await send_device({"type": "tts", "state": "stop"})
            await send_device({"relay_type": "control", "command": "clear_audio"})
        listening = False
        await send_device({"type": "status", "state": "tool_running", "tool": name})
        result = await asyncio.to_thread(call_server_tool, name, args)
        listening = True
        message = result.get("message") or "工具已执行"
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if name == "server.music.play" and result.get("ok") and isinstance(data, dict):
            stream = data.get("music_stream")
            if isinstance(stream, dict):
                pending_music_stream = stream
                suppress_next_done_music = True
                title = str(stream.get("title") or "音乐").strip()
                if memory_enabled():
                    add_music_history(title, str(args.get("query") or ""), user_text)
                message = f"马上播放{title}"
        elif name == "server.music.pause":
            await stop_music_stream("pause")
        try:
            if name == "server.weather.query":
                prompt = f"请直接照读这句话，不要改写，不要加字：{message}"
            elif name.startswith("server.music."):
                prompt = f"请直接说：{message}"
            else:
                prompt = f"工具结果：{message}。只用中文口语回复，不超过16字。"
            await send_json_ws(dashscope, {"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}})
            await send_json_ws(dashscope, {"type": "response.create"})
        except Exception as exc:
            await send_device({"type": "error", "message": f"工具播报失败: {exc}"})

    try:
        update_relay_state(last_event="sending runtime config")
        await send_runtime_to_device()
        update_relay_state(last_event="runtime config sent")
        api_key = await wait_for_required_api_key()
        while True:
            try:
                dashscope = await dashscope_connect(f"{DASHSCOPE_WS_URL}?model={model}", {"Authorization": f"Bearer {api_key}"})
                break
            except Exception as exc:
                message = f"DashScope connect failed: {exc}"
                update_relay_state(last_error=message, last_event="dashscope connect failed")
                print(f"[DashScope error] {exc}")
                try:
                    await send_device({"type": "error", "message": message})
                    await send_device({"type": "status", "state": "cloud_error", "message": message})
                except Exception:
                    raise
                await asyncio.sleep(3.0)
                cfg = ensure_pairing_token(load_config())
                api_key = await wait_for_required_api_key()
        update_relay_state(dashscope_connected=True, last_error="", last_event="dashscope connected")
        await send_json_ws(dashscope, {"type": "session.update", "session": {
            "model": model, "modalities": ["text", "audio"], "voice": voice,
            "input_audio_format": "pcm", "output_audio_format": "pcm",
            "instructions": instructions,
            "turn_detection": {"type": "server_vad", "threshold": 0.5, "silence_duration_ms": 300, "prefix_padding_ms": 50},
        }})
        await send_runtime_to_device()
        await send_device({"type": "status", "state": "ready", "voice": voice, "model": model})
        if wake_word_enabled():
            await send_device({"type": "status", "state": "sleeping", "wake_word": wake_word_value()})
        print(f"[DashScope] ready model={model} voice={voice} wake_word={wake_word_value() if wake_word_enabled() else 'off'} interrupt={interrupt_enabled()}")

        async def from_esp32() -> None:
            nonlocal listening, esp32_buffer_ms
            frames = 0
            async for msg in esp32:
                if isinstance(msg, bytes):
                    if not msg or not listening:
                        continue
                    await send_json_ws(dashscope, {"type": "input_audio_buffer.append", "audio": base64.b64encode(msg).decode("ascii")})
                    frames += 1
                    if frames % 50 == 1:
                        update_relay_state(audio_frames_from_esp32=frames, last_event=f"audio frames {frames}")
                    continue
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                msg_type = data.get("type") or data.get("relay_type") or ""
                if msg_type == "hello":
                    update_relay_state(audio_header_enabled=True, last_event="device hello audio_header=required")
                    await send_runtime_to_device()
                elif msg_type == "buffer_status":
                    esp32_buffer_ms = int(data.get("buffer_ms") or 0)
                    update_relay_state(
                        esp32_buffer_ms=esp32_buffer_ms,
                        esp32_buffer_pct=int(data.get("buffer_pct") or 0),
                        esp32_underflows=int(data.get("underflows") or 0),
                        esp32_overruns=int(data.get("overruns") or 0),
                        esp32_buffer_primed=bool(data.get("primed")),
                        esp32_rebuffers=int(data.get("rebuffer_count") or 0),
                        esp32_packets=int(data.get("packets") or 0),
                        esp32_audio_header=bool(data.get("audio_header")),
                        esp32_last_type=str(data.get("last_type") or ""),
                        esp32_last_seq=int(data.get("last_seq") or 0),
                    )
                elif msg_type == "listen":
                    listening = data.get("state") != "stop"
                elif msg_type == "abort":
                    await stop_current_response(data.get("reason") or "device_abort")

        async def from_dashscope() -> None:
            nonlocal listening, tts_started, tts_playback_started_at, current_turn_allowed, wake_response_cancelled, wake_active_until, pending_music_stream, music_task, suppress_next_done_music
            audio_sent = 0
            async for raw in dashscope:
                data = json.loads(raw)
                event_type = data.get("type", "")
                if event_type == "input_audio_buffer.speech_started":
                    current_turn_allowed = (not wake_word_enabled()) or wake_is_active()
                    wake_response_cancelled = False
                    if interrupt_enabled() and (current_turn_allowed or tts_started):
                        await stop_current_response("user_speech_started")
                if event_type == "conversation.item.input_audio_transcription.completed":
                    text = data.get("transcript", "")
                    await send_device({"type": "stt", "text": text})
                    if wake_word_enabled():
                        wake_now = text_has_wake_word(text, wake_word_value())
                        music_active = music_task is not None and not music_task.done()
                        if (wake_now or wake_is_active()) and text_has_sleep_command(text):
                            wake_active_until = 0.0
                            current_turn_allowed = False
                            await stop_music_stream("sleep_command")
                            await cancel_sleeping_response("sleep_command")
                            continue
                        if music_active and not wake_now:
                            current_turn_allowed = False
                            print(f"[Wake] music ignored, say {wake_word_value()}: {text}")
                            continue
                        if wake_now:
                            if music_active:
                                await stop_music_stream("wake_word")
                            await mark_awake("wake_word")
                        elif wake_is_active():
                            await mark_awake("timeout_extend")
                        else:
                            current_turn_allowed = False
                            await cancel_sleeping_response("wake_word_required")
                            print(f"[Wake] ignored, say {wake_word_value()}: {text}")
                            continue
                    else:
                        current_turn_allowed = True
                    if memory_enabled():
                        add_memory_turn("user", text)
                    if mcp_enabled():
                        tool_call = detect_server_tool_call(text)
                        if tool_call:
                            await run_server_tool_for_user(tool_call[0], tool_call[1], text)
                    print(f"[User] {text}")
                elif event_type == "response.audio_transcript.delta":
                    if not (wake_word_enabled() and not current_turn_allowed):
                        await send_device({"type": "llm", "text_delta": data.get("delta", "")})
                elif event_type == "response.audio_transcript.done":
                    if not (wake_word_enabled() and not current_turn_allowed):
                        text = data.get("text", "")
                        await send_device({"type": "tts", "state": "sentence_start", "text": text})
                        if memory_enabled():
                            add_memory_turn("assistant", text)
                elif event_type == "error":
                    update_relay_state(last_error=json.dumps(data, ensure_ascii=False))
                    await send_device({"type": "error", "message": data.get("message") or json.dumps(data, ensure_ascii=False)})

                if event_type == "response.audio.delta":
                    if wake_word_enabled() and not current_turn_allowed:
                        if not wake_response_cancelled:
                            await cancel_sleeping_response("wake_word_required")
                        continue
                    if not tts_started:
                        tts_started = True
                        tts_playback_started_at = time.monotonic()
                        listening = bool(interrupt_enabled())
                        downsample_state["phase"] = 0
                        audio_sent = 0
                        await send_device({"type": "tts", "state": "start"})
                        if interrupt_enabled():
                            await send_device({"relay_type": "control", "command": "start_record"})
                        else:
                            await send_device({"relay_type": "control", "command": "stop_record"})
                    pcm24 = base64.b64decode(data.get("delta", ""))
                    pcm16 = downsample_24k_to_16k_stream(pcm24, downsample_state)
                    if pcm16:
                        await send_audio_frame(AUDIO_TYPE_TTS, pcm16, "tts")
                        audio_sent += len(pcm16)
                        update_relay_state(audio_bytes_to_esp32=audio_sent, last_event=f"streaming audio {audio_sent} bytes")
                elif event_type in ("response.audio.done", "response.done"):
                    should_play_music = False
                    if tts_started:
                        suppress_next_done_music = False
                        tts_started = False
                        await send_device({"type": "tts", "state": "stop"})
                        tts_duration = audio_sent / MUSIC_PCM_BYTES_PER_SECOND if audio_sent else 0.0
                        tts_elapsed = max(0.0, time.monotonic() - tts_playback_started_at)
                        wait_for_playback = min(8.0, max(0.45, tts_duration - tts_elapsed + 0.65))
                        await asyncio.sleep(wait_for_playback)
                        should_play_music = True
                    elif event_type == "response.done":
                        if suppress_next_done_music:
                            suppress_next_done_music = False
                        else:
                            should_play_music = True
                    if should_play_music and pending_music_stream:
                        stream = pending_music_stream
                        pending_music_stream = None
                        await start_music_stream_task(stream)
                    elif should_play_music:
                        listening = True
                        await send_device({"relay_type": "control", "command": "start_record"})

        await asyncio.gather(from_esp32(), from_dashscope())
    except Exception as exc:
        update_relay_state(last_error=str(exc), last_event="relay error")
        print(f"[Relay error] {exc}")
        try:
            await send_json_ws(esp32, {"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        try:
            if music_task is not None and not music_task.done():
                music_task.cancel()
        except Exception:
            pass
        if dashscope is not None:
            await dashscope.close()
        clear_active_device(session_id)
        last_error = str(relay_status().get("last_error") or "")
        update_relay_state(esp32_connected=False, dashscope_connected=False, last_event="esp32 disconnected")
        if last_error:
            print(f"[ESP32] disconnected reason={last_error}")
        else:
            print("[ESP32] disconnected")


async def relay_entry(esp32: Any, path: str | None = None) -> None:
    update_relay_state(last_event=f"relay entry {websocket_debug_info(esp32, path)}", last_error="")
    try:
        await relay_session(esp32, path)
    except Exception as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        tb = traceback.format_exc(limit=6)
        message = f"relay handler crash: {detail}"
        update_relay_state(last_error=message, last_event="relay handler crash", relay_debug=tb)
        print(f"[Relay crash] {tb}")
        try:
            await send_json_ws(esp32, {"type": "error", "message": message})
            await asyncio.sleep(0.5)
        except Exception:
            pass
        try:
            await esp32.close(code=1011, reason=message[:120])
        except Exception:
            pass



class AppHandler(BaseHTTPRequestHandler):
    server_version = "N.E.K.O_ESP32/2.0"
    http_port = DEFAULT_HTTP_PORT
    relay_port = DEFAULT_RELAY_PORT

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def is_local_client(self) -> bool:
        return is_loopback_address(str(self.client_address[0] if self.client_address else ""))

    def is_api_authorized(self) -> bool:
        if self.is_local_client():
            return True
        return pairing_token_valid(extract_http_token(self))

    def require_api_auth(self) -> bool:
        if self.is_api_authorized():
            return True
        self.send_json({"ok": False, "error": "unauthorized: missing or invalid pairing token"}, HTTPStatus.UNAUTHORIZED)
        return False

    def send_common_headers(self, content_type: str, length: int, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        cors = allowed_cors_origin(self.headers.get("Origin", ""), self.headers.get("Host", ""))
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Neko-Token, X-Pairing-Token, Authorization")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        code, body = json_bytes(payload, status)
        self.send_common_headers("application/json; charset=utf-8", len(body), code)
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_common_headers("text/plain; charset=utf-8", 0, HTTPStatus.NO_CONTENT)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self.require_api_auth():
            return
        if path in ("/", "/index.html"):
            html_text = (PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
            body = html_text.encode("utf-8")
            self.send_common_headers("text/html; charset=utf-8", len(body), HTTPStatus.OK)
            self.wfile.write(body)
        elif path == "/api/config":
            cfg = ensure_pairing_token(load_config())
            self.send_json({"ok": True, "config": public_config(cfg), "voices": DEFAULT_VOICES, "default_instructions": DEFAULT_INSTRUCTIONS, "server": server_info(), "relay": relay_status()})
        elif path == "/api/status":
            self.send_json({"ok": True, "server": server_info(), "relay": relay_status()})
        elif path == "/api/tools":
            self.send_json({"ok": True, "tools": SERVER_MCP_TOOLS})
        elif path == "/api/location":
            self.send_json({"ok": True, "location": public_location()})
        elif path == "/api/memory":
            self.send_json({"ok": True, "memory": load_memory()})
        else:
            self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self.require_api_auth():
            return
        try:
            if path == "/api/config":
                incoming = read_json_body(self)
                cfg = ensure_pairing_token(load_config())
                for key in ("voice", "model", "volume", "instructions", "mcp_enabled", "memory_enabled", "interrupt_enabled", "wake_word_enabled", "wake_word", "wake_timeout_seconds", "default_weather_city"):
                    if key in incoming:
                        cfg[key] = incoming[key]
                api_key = (incoming.get("api_key") or incoming.get("dashscope_api_key") or "").strip()
                if api_key:
                    cfg["api_key"] = cfg["dashscope_api_key"] = api_key
                cfg = save_config(cfg)
                esp32_sync = sync_active_runtime_config(cfg)
                self.send_json({"ok": True, "config": public_config(cfg), "server": server_info(), "esp32": esp32_sync})
            elif path == "/api/location":
                self.send_json({"ok": True, "location": save_location(read_json_body(self))})
            elif path == "/api/tools/call":
                incoming = read_json_body(self)
                tool_name = incoming.get("name", "")
                tool_args = incoming.get("arguments") or incoming.get("args") or {}
                result = call_server_tool(tool_name, tool_args)
                esp32_result = apply_tool_result_to_active_esp32(tool_name, result, tool_args)
                self.send_json({"ok": True, "tool": result, "esp32": esp32_result})
            elif path == "/api/memory":
                incoming = read_json_body(self)
                memory = save_memory({"notes": []}) if (incoming.get("action") or "add").lower() == "clear" else add_memory_note(incoming.get("text", ""))
                self.send_json({"ok": True, "memory": memory})
            elif path == "/api/clone":
                self.handle_clone()
            else:
                self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_clone(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_UPLOAD_BYTES:
            self.send_json({"ok": False, "error": "audio file is too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(length)}, keep_blank_values=True)
        if "audio" not in form:
            raise ValueError("missing audio file")
        file_item = form["audio"][0] if isinstance(form["audio"], list) else form["audio"]
        filename = sanitize_filename(file_item.filename or "voice.mp3")
        audio_bytes = file_item.file.read()
        cfg = load_config()
        api_key = (form.getfirst("api_key") or cfg.get("api_key") or cfg.get("dashscope_api_key") or "").strip()
        if not api_key:
            raise ValueError("missing api key")
        preferred_name = (form.getfirst("voice_name") or "esp32_voice").strip()
        target_model = (form.getfirst("target_model") or cfg.get("model") or DEFAULT_MODEL).strip()
        saved = UPLOAD_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-{filename}"
        saved.write_bytes(audio_bytes)
        clone = clone_voice(api_key, audio_bytes, filename, preferred_name, target_model)
        if not clone.get("ok") or not clone.get("voice"):
            self.send_json({"ok": False, "error": clone.get("error") or "clone failed", "clone": clone}, HTTPStatus.BAD_REQUEST)
            return
        cfg["api_key"] = cfg["dashscope_api_key"] = api_key
        cfg["voice"] = cfg["last_cloned_voice"] = clone["voice"]
        try:
            cfg["last_clone_file"] = str(saved.relative_to(BASE_DIR))
        except ValueError:
            cfg["last_clone_file"] = str(saved)
        cfg = save_config(cfg)
        self.send_json({"ok": True, "voice": clone["voice"], "clone": clone, "config": public_config(cfg)})


def start_http_server(host: str, port: int) -> ThreadingHTTPServer:
    AppHandler.http_port = port
    httpd = ThreadingHTTPServer((host, port), AppHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def run_async(host: str, http_port: int, relay_port: int) -> None:
    ensure_dirs()
    AppHandler.relay_port = relay_port
    httpd = start_http_server(host, http_port)
    print("N.E.K.O_ESP32 server")
    print(f"Config Local: http://127.0.0.1:{http_port}")
    print(f"Config LAN:   http://{lan_ip()}:{http_port}")
    print(f"Relay:        ws://{lan_ip()}:{relay_port}/")
    if websockets is None:
        print("Missing dependency: pip3 install -r requirements.txt")
    async with websockets.serve(relay_entry, host, relay_port, max_size=None, ping_interval=None, close_timeout=2):
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            httpd.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--relay-port", type=int, default=DEFAULT_RELAY_PORT)
    args = parser.parse_args()
    if websockets is None:
        print("python websockets package missing. Run: pip3 install -r requirements.txt")
        raise SystemExit(1)
    asyncio.run(run_async(args.host, args.port, args.relay_port))


if __name__ == "__main__":
    main()
