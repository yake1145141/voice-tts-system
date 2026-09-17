"""对真实 AstrBot 运行环境的集成校验（可选）。

与 test_plugin_flow.py（使用桩）不同，本脚本会真实导入 AstrBot，
用于确认插件使用的 API 在当前 AstrBot 版本中确实存在且行为一致：

  1. `astrbot.api` 下所有被插件导入的符号都存在；
  2. 插件模块被导入后，钩子 / 指令被正确注册进 star_handlers_registry；
  3. `_conf_schema.json` 能被 AstrBot 解析成配置并传给插件实例；
  4. 用真实的 AstrMessageEvent / MessageEventResult / Plain / Record
     跑通「文本回复 -> 语音回复」的完整替换流程。

运行（需要在已安装 AstrBot 的 Python 环境中）：
    python tests/test_astrbot_real_api.py

若当前环境没有安装 AstrBot，脚本会打印提示并以 0 退出。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "astrbot_plugin_voice_reply"
MODULE_NAME = "data.plugins.astrbot_plugin_voice_reply.main"

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  [PASS] {message}")
    else:
        print(f"  [FAIL] {message}")
        FAILURES.append(message)


def import_real_astrbot():
    """返回 AstrBot 相关对象；未安装时返回 None。"""
    try:
        import astrbot  # noqa: F401
        from astrbot.api import AstrBotConfig
        from astrbot.api.event import AstrMessageEvent, filter as astr_filter
        from astrbot.api.message_components import Plain, Record
        from astrbot.api.platform import (
            AstrBotMessage,
            MessageMember,
            MessageType,
            PlatformMetadata,
        )
        from astrbot.api.star import Context, Star, StarTools
        from astrbot.core.message.message_event_result import MessageEventResult
        from astrbot.core.star.star_handler import EventType, star_handlers_registry
    except Exception as exc:  # noqa: BLE001
        print(f"跳过：当前环境未安装可用的 AstrBot（{exc}）")
        return None

    try:
        from importlib.metadata import version

        astrbot_version = version("astrbot")
    except Exception:  # noqa: BLE001
        astrbot_version = "unknown"

    return types.SimpleNamespace(
        AstrBotConfig=AstrBotConfig,
        AstrMessageEvent=AstrMessageEvent,
        filter=astr_filter,
        Plain=Plain,
        Record=Record,
        AstrBotMessage=AstrBotMessage,
        MessageMember=MessageMember,
        MessageType=MessageType,
        PlatformMetadata=PlatformMetadata,
        Context=Context,
        Star=Star,
        StarTools=StarTools,
        MessageEventResult=MessageEventResult,
        EventType=EventType,
        star_handlers_registry=star_handlers_registry,
        version=astrbot_version,
    )


def load_plugin_as_astrbot_module():
    """以 AstrBot 实际的模块名格式（data.plugins.<name>.main）加载插件。"""
    package_name = MODULE_NAME.rsplit(".", 1)[0]
    pkg = types.ModuleType(package_name)
    pkg.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[package_name] = pkg

    spec = importlib.util.spec_from_file_location(MODULE_NAME, PLUGIN_DIR / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class FakeHTTPResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"", headers=None, payload=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self._payload = payload
        self.reason_phrase = "OK"

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="ignore")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHTTPClient:
    WAV = b"RIFF" + b"\x00" * 4000

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def post(self, url: str, json=None, headers=None):  # noqa: A002
        self.calls.append(url)
        if url.endswith("/api/tts/file"):
            return FakeHTTPResponse(200, self.WAV, {"content-type": "audio/wav"})
        return FakeHTTPResponse(
            200,
            b'{"success": true}',
            {"content-type": "application/json"},
            {"success": True, "audio_url": "http://server/audio/x.wav"},
        )

    async def get(self, url: str, timeout=None):  # noqa: A002
        return FakeHTTPResponse(
            200,
            b'{"success": true}',
            {"content-type": "application/json"},
            {"success": True, "status": "ok", "engine": {"device": "cuda:0", "model": "MyVoice.pth"}},
        )

    async def aclose(self) -> None:
        pass


def build_event(api, text: str):
    message = api.AstrBotMessage()
    message.type = api.MessageType.FRIEND_MESSAGE
    message.self_id = "1"
    message.session_id = "10001"
    message.message_id = "1"
    message.sender = api.MessageMember(user_id="10001", nickname="tester")
    message.message = [api.Plain(text)]
    message.message_str = text
    message.raw_message = None
    platform = api.PlatformMetadata(name="aiocqhttp", description="test", id="aiocqhttp")
    event = api.AstrMessageEvent(text, message, platform, "10001")
    event.is_wake = True
    return event


async def main_async() -> int:
    print("=== 真实 AstrBot API 集成校验 ===")
    # AstrBot 的数据目录基于当前工作目录创建，这里切到临时目录，避免污染仓库
    os.chdir(tempfile.mkdtemp(prefix="astrbot-real-api-"))
    api = import_real_astrbot()
    if api is None:
        return 0
    print(f"  AstrBot 版本: {api.version}")

    # 1) API 符号存在性
    print("API 符号")
    check(hasattr(api.filter, "command"), "filter.command 存在")
    check(hasattr(api.filter, "permission_type"), "filter.permission_type 存在")
    check(hasattr(api.filter, "on_decorating_result"), "filter.on_decorating_result 存在（发送消息前钩子）")
    check(hasattr(api.filter.PermissionType, "ADMIN"), "PermissionType.ADMIN 存在")
    for name in ("get_result", "set_result", "set_extra", "get_extra", "plain_result", "send"):
        check(hasattr(api.AstrMessageEvent, name), f"AstrMessageEvent.{name} 存在")
    for name in ("put_kv_data", "get_kv_data", "initialize", "terminate"):
        check(hasattr(api.Star, name), f"Star.{name} 存在")
    check(hasattr(api.StarTools, "get_data_dir"), "StarTools.get_data_dir 存在")

    # 2) 加载插件模块并检查 handler 注册
    print("插件加载与 handler 注册")
    module = load_plugin_as_astrbot_module()
    handlers = [
        h
        for h in api.star_handlers_registry
        if getattr(h, "handler_module_path", "") == MODULE_NAME
    ]
    hook_handlers = [h for h in handlers if h.event_type == api.EventType.OnDecoratingResultEvent]
    command_handlers = [h for h in handlers if h.event_type == api.EventType.AdapterMessageEvent]
    check(len(hook_handlers) == 1, f"注册了 1 个 on_decorating_result 钩子（实际 {len(hook_handlers)}）")
    check(len(command_handlers) == 1, f"注册了 1 个指令 handler（实际 {len(command_handlers)}）")
    if command_handlers:
        command_filter = command_handlers[0].event_filters[0]
        check(
            "voice" in command_filter.get_complete_command_names(),
            "指令名为 voice（/voice on|off|status）",
        )
        check(
            bool(command_handlers[0].event_filters)
            and any(
                type(f).__name__ == "PermissionTypeFilter" for f in command_handlers[0].event_filters
            ),
            "指令带有官方权限过滤器（管理员限定）",
        )

    # 3) 配置 Schema
    print("配置 Schema")
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    check("tts_server" in schema and "voice" in schema, "Schema 顶层包含 tts_server / voice")
    check(
        schema["voice"]["items"]["max_text_length"]["default"] == 300,
        "voice.max_text_length 默认 300",
    )
    with tempfile.TemporaryDirectory() as tmp:
        config = api.AstrBotConfig(config_path=str(Path(tmp) / "cfg.json"), schema=schema)
        check(config.get("voice", {}).get("enabled") is True, "配置对象生成默认值 enabled=true")
        check(
            config.get("tts_server", {}).get("url") == "http://127.0.0.1:8080",
            "配置对象生成默认值 url",
        )

        # 4) 真实事件下的完整流程
        print("真实事件流程")
        # 真实运行时 context 由 AstrBot 注入；这里只需要一个占位对象
        plugin = module.VoiceReplyPlugin(context=types.SimpleNamespace(), config=config)
        await plugin.initialize()
        if plugin._client is not None:
            await plugin._client.aclose()
        fake = FakeHTTPClient()
        plugin._client = fake

        event = build_event(api, "你好呀！（开心地笑）")
        event.set_result(api.MessageEventResult().message("你好呀！（开心地笑）"))
        await plugin.on_decorating_result(event)
        chain = event.get_result().chain
        check(len(chain) == 1, f"消息链只剩 1 段（实际 {len(chain)}）")
        check(isinstance(chain[0], api.Record), "文本段被替换为真实的 Record 语音段")
        check(bool(fake.calls) and fake.calls[0].endswith("/api/tts/file"), "调用了 /api/tts/file")
        if isinstance(chain[0], api.Record):
            audio = Path(str(chain[0].file))
            check(audio.exists() and audio.stat().st_size > 512, "音频文件已下载到本地缓存")
            # text 字段是 AstrBot 4.28 新增的（用于降级时的文案），旧版本会被忽略
            record_text = getattr(chain[0], "text", None)
            check(
                record_text in (None, "你好呀！"),
                f"语音段附带文本（当前版本: {record_text!r}）",
            )

        # 失败降级：真实事件下原始文字必须保留
        event2 = build_event(api, "测试降级（括号）")
        event2.set_result(api.MessageEventResult().message("测试降级（括号）"))

        async def boom(*args, **kwargs):
            raise RuntimeError("模拟 TTS 崩溃")

        plugin._synthesize = boom  # type: ignore[method-assign]
        await plugin.on_decorating_result(event2)
        chain2 = event2.get_result().chain
        check(
            isinstance(chain2[0], api.Plain) and chain2[0].text == "测试降级（括号）",
            "TTS 异常时原始文字回复完好无损",
        )

        # 指令返回值类型
        cmd_event = build_event(api, "/voice status")
        results = [item async for item in plugin.voice_command(cmd_event, "status")]
        check(
            bool(results) and isinstance(results[0], api.MessageEventResult),
            "指令返回官方 MessageEventResult",
        )
        check(
            "语音回复状态" in results[0].chain[0].text,
            "/voice status 输出状态文本",
        )
        await plugin.terminate()

    print()
    if FAILURES:
        print(f"❌ 失败 {len(FAILURES)} 项:")
        for item in FAILURES:
            print(f"   - {item}")
        return 1
    print("✅ 真实 AstrBot 环境下全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
