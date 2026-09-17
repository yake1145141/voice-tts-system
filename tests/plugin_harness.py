"""插件自测公共环境：注入 AstrBot 桩、加载插件模块、提供假 HTTP 客户端与断言工具。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

import httpx  # noqa: E402

PLUGIN_PATH = ROOT / "astrbot_plugin_voice_reply" / "main.py"


def load_plugin_module():
    """按文件路径加载插件 main.py（模拟 AstrBot 的动态加载）。"""
    spec = importlib.util.spec_from_file_location("voice_reply_plugin_main", PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin_mod = load_plugin_module()
stub = astrbot_stub

FAILURES: list[str] = []


def check(condition: bool, message: str) -> bool:
    if condition:
        print(f"  [PASS] {message}")
    else:
        print(f"  [FAIL] {message}")
        FAILURES.append(message)
    return bool(condition)


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b"",
        headers: dict | None = None,
        payload=None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self._payload = payload
        self.reason_phrase = "OK" if status_code == 200 else "Error"

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="ignore")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


WAV_BYTES = b"RIFF" + b"\x00" * 3000


class FakeClient:
    """模拟 httpx.AsyncClient，记录请求并按 mode 返回不同结果。"""

    def __init__(self, mode: str = "file_ok", audio_url: str = "http://server/audio/abc.wav") -> None:
        self.mode = mode
        self.audio_url = audio_url
        self.calls: list[tuple[str, str]] = []

    async def post(self, url: str, json=None, headers=None):  # noqa: A002
        self.calls.append(("POST", url))
        if self.mode == "connect_error":
            raise httpx.ConnectError("connection refused")
        if self.mode == "timeout":
            raise httpx.ReadTimeout("timed out")
        if self.mode == "http_500":
            payload = {"success": False, "error": {"code": "rvc_failed", "message": "RVC 崩了"}}
            return FakeResponse(500, b'{"success": false}', {"content-type": "application/json"}, payload)
        if self.mode == "unauthorized":
            payload = {"success": False, "error": {"code": "unauthorized", "message": "bad key"}}
            return FakeResponse(401, b'{"success": false}', {"content-type": "application/json"}, payload)
        if self.mode == "empty_audio":
            return FakeResponse(200, b"", {"content-type": "audio/wav"})
        if url.endswith("/api/tts/file"):
            if self.mode == "url_only":
                return FakeResponse(404, b'{"detail":"not found"}', {"content-type": "application/json"})
            return FakeResponse(200, WAV_BYTES, {"content-type": "audio/wav"})
        return FakeResponse(
            200,
            b'{"success": true}',
            {"content-type": "application/json"},
            {"success": True, "audio_url": self.audio_url, "filename": "abc.wav"},
        )

    async def get(self, url: str, timeout=None):  # noqa: A002
        self.calls.append(("GET", url))
        if self.mode == "connect_error":
            raise httpx.ConnectError("connection refused")
        return FakeResponse(
            200,
            b'{"success": true}',
            {"content-type": "application/json"},
            {
                "success": True,
                "status": "ok",
                "engine": {
                    "device": "cuda:0",
                    "model": "/models/MyVoice.pth",
                    "speaker": "zh-CN-YunxiNeural",
                    "pitch": 5,
                },
                "storage": {"files": 1, "expire_minutes": 10.0, "expire_seconds": 600.0},
            },
        )

    async def aclose(self) -> None:
        pass


CONFIG = {
    "tts_server": {"url": "http://127.0.0.1:8080", "api_key": "secret", "delivery": "auto"},
    "voice": {
        "enabled": True,
        "max_text_length": 30,
        "timeout": 10,
        "max_concurrent": 2,
        "keep_text": False,
        "only_llm_result": False,
        "cleanup_markdown": True,
        "cache_expire_minutes": 30,
        "skip_platforms": ["qq_official", "qq_official_webhook", "dingtalk", "lark"],
    },
}


def config_with(**voice_overrides) -> dict:
    return {**CONFIG, "voice": {**CONFIG["voice"], **voice_overrides}}


async def make_plugin(config: dict | None = None, mode: str = "file_ok"):
    plugin = plugin_mod.VoiceReplyPlugin(
        context=astrbot_stub.Context(),
        config=config or dict(CONFIG),
    )
    await plugin.initialize()
    if plugin._client is not None:
        await plugin._client.aclose()
    client = FakeClient(mode=mode)
    plugin._client = client
    return plugin, client


def event_for(text: str, platform: str = "aiocqhttp"):
    return astrbot_stub.AstrMessageEvent(
        message_str=text,
        platform=platform,
        chain=[astrbot_stub.Plain(text)],
    )


def report(title: str) -> int:
    print()
    if FAILURES:
        print(f"❌ {title}：失败 {len(FAILURES)} 项")
        for item in FAILURES:
            print(f"   - {item}")
        return 1
    print(f"✅ {title}：全部通过")
    return 0
