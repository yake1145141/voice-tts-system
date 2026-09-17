"""AstrBot 插件 API 的最小桩实现，用于在没有完整 AstrBot 环境时自测插件逻辑。

仅实现 ``astrbot_plugin_voice_reply/main.py`` 实际用到的接口，
签名与 AstrBot 4.x 官方 API 保持一致（见 astrbot/api/*）。
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path


class Plain:
    def __init__(self, text: str = "", **kwargs) -> None:
        self.text = text
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Plain({self.text!r})"


class Record:
    """AstrBot 的语音消息段。"""

    def __init__(self, file: str | None = "", **kwargs) -> None:
        self.file = file
        for key, value in kwargs.items():
            setattr(self, key, value)

    @staticmethod
    def fromFileSystem(path: str, **kwargs) -> "Record":
        return Record(file=Path(path).as_uri(), path=str(path), **kwargs)

    @staticmethod
    def fromURL(url: str, **kwargs) -> "Record":
        return Record(file=url, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Record({self.file!r})"


class AstrBotConfig(dict):
    """AstrBot 插件配置对象（dict 子类）。"""

    def save_config(self) -> None:
        pass


class _Filter:
    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"
        GROUP_ADMIN = "group_admin"
        SHARED_GROUP_ADMIN = "shared_group_admin"

    @staticmethod
    def _keep(func):
        return func

    def command(self, *_args, **_kwargs):
        return self._keep

    def permission_type(self, *_args, **_kwargs):
        return self._keep

    def on_decorating_result(self, *_args, **_kwargs):
        return self._keep

    def event_message_type(self, *_args, **_kwargs):
        return self._keep


class AstrMessageEvent:
    """消息事件桩：只实现插件使用的字段与方法。"""

    def __init__(
        self,
        message_str: str = "",
        platform: str = "aiocqhttp",
        sender_id: str = "10086",
        chain: list | None = None,
    ) -> None:
        self.message_str = message_str
        self._platform = platform
        self._sender_id = sender_id
        self._extras: dict = {}
        self.sent: list = []
        self._result = None
        if chain is not None:
            self.set_result(chain)

    # ---- 结果 ----
    def set_result(self, chain: list) -> None:
        result = types.SimpleNamespace(chain=list(chain))
        result.is_model_result = lambda: True
        result.result_content_type = types.SimpleNamespace(name="GENERAL_RESULT")
        self._result = result

    def get_result(self):
        return self._result

    # ---- 基本信息 ----
    def get_platform_name(self) -> str:
        return self._platform

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return "123456"

    def set_extra(self, key, value) -> None:
        self._extras[key] = value

    def get_extra(self, key: str | None = None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def is_admin(self) -> bool:
        return True

    def plain_result(self, text: str):
        return ("plain", text)

    async def send(self, message) -> None:
        self.sent.append(message)


class Star:
    """插件基类桩（对应 astrbot.api.star.Star）。"""

    plugin_id = "stub_plugin_id"
    name = "astrbot_plugin_voice_reply"

    def __init__(self, context=None, config=None) -> None:
        self.context = context
        self.logger = logging.getLogger("astrbot.plugin.test")
        self._kv: dict = {}

    async def put_kv_data(self, key, value) -> None:
        self._kv[key] = value

    async def get_kv_data(self, key, default=None):
        return self._kv.get(key, default)

    async def delete_kv_data(self, key) -> None:
        self._kv.pop(key, None)

    async def initialize(self) -> None:  # pragma: no cover
        pass

    async def terminate(self) -> None:  # pragma: no cover
        pass


class Context:  # pragma: no cover - 桩
    pass


class StarTools:  # pragma: no cover - 桩
    @classmethod
    def get_data_dir(cls, plugin_name: str | None = None):
        raise RuntimeError("stub: 使用临时目录")


def install() -> None:
    """把桩模块注入 sys.modules。"""
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot")
    api.AstrBotConfig = AstrBotConfig

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = _Filter()
    event_mod.AstrMessageEvent = AstrMessageEvent

    components_mod = types.ModuleType("astrbot.api.message_components")
    components_mod.Plain = Plain
    components_mod.Record = Record

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    star_mod.register = lambda *a, **k: (lambda cls: cls)

    astrbot_mod = types.ModuleType("astrbot")

    sys.modules.update(
        {
            "astrbot": astrbot_mod,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.message_components": components_mod,
            "astrbot.api.star": star_mod,
        },
    )
    api.event = event_mod
    api.message_components = components_mod
    api.star = star_mod
