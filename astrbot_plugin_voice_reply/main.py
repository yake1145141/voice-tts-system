"""AstrBot 插件：AI 自动语音回复（tts-with-rvc）。

工作原理（官方事件钩子 ``on_decorating_result``，即「发送消息前」钩子）：

    AI 文本回复 -> 过滤括号/中括号内容 -> 长度检查 -> 调用 tts-with-rvc HTTP 服务
                -> 把文本消息段替换成语音消息段 -> AstrBot 发送语音

只要上述任何一步失败（服务连不上、超时、RVC 失败、文本过长、过滤后为空、
平台不支持语音……），插件都会保持原始文本消息不变，绝不会让 AI 的回复丢失。

开发规范参考：https://docs.astrbot.app/dev/star/plugin-new.html
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_voice_reply"
PLUGIN_VERSION = "1.0.0"
LOG = "[VoiceReply]"

# 事件标记：用于防止插件自己产生的消息被再次处理（无限循环）
FLAG_SKIP = "_voice_reply_skip"        # 插件指令自身的回复不转语音
FLAG_HANDLED = "_voice_reply_handled"  # 该事件已经处理过，避免重复合成

# 默认配置（当 _conf_schema.json 缺失或用户未配置时使用）
DEFAULT_CONFIG: dict[str, Any] = {
    "tts_server": {
        "url": "http://127.0.0.1:8080",
        "api_key": "",
        "delivery": "auto",
    },
    "voice": {
        "enabled": True,
        "max_text_length": 300,
        "timeout": 60,
        "max_concurrent": 2,
        "keep_text": False,
        "only_llm_result": False,
        "cleanup_markdown": True,
        "cache_expire_minutes": 60,
        "skip_platforms": [
            "qq_official",
            "qq_official_webhook",
            "dingtalk",
            "lark",
        ],
    },
}


# ===========================================================================
# 文本处理：括号过滤
# ===========================================================================

# 成对括号：开括号 -> 闭括号（支持中文/英文小括号与方括号）
BRACKET_PAIRS = {"（": "）", "(": ")", "[": "]", "【": "】"}
BRACKET_CLOSERS = set(BRACKET_PAIRS.values())

# 零宽字符与方向控制符，容易让 TTS 出错，直接删除
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
# 行内多余空白
_INLINE_SPACE_RE = re.compile(r"[ \t\u3000]+")
# Markdown 装饰符号（只去掉符号本身，不删除内容）
_MD_RE = re.compile(r"[*_~`]+")
# 行首的 Markdown 结构标记： # / ## / > / - / 1. 等
_MD_LEAD_RE = re.compile(r"^\s*(?:#{1,6}\s*|>\s*|[-+]\s+|\d+[.、)]\s+)+")


def strip_bracket_content(text: str) -> str:
    """删除括号及其内部内容（支持嵌套）。

    使用字符扫描 + 深度计数，而不是简单正则，因此可以正确处理：
        "你好！（开心地说：[笑]）" -> "你好！"
        "Hello! (smile)"          -> "Hello!"
        "你好！[系统提示]"         -> "你好！"
        "你好！【开心】"           -> "你好！"

    规则：
    * 遇到任意开括号（ ( [ 【 ）进入「括号内部」，内部内容一律丢弃；
    * 深度大于 0 时遇到任意闭括号，深度减一；
    * 没有配对的闭括号按普通文本保留（例如 "测试]」"）；
    * 未闭合的开括号视为丢弃到文本结尾（例如 "（开心" -> ""）。
    """
    if not text:
        return ""

    result: list[str] = []
    depth = 0
    for char in text:
        if char in BRACKET_PAIRS:
            depth += 1
            continue
        if char in BRACKET_CLOSERS:
            if depth > 0:
                depth -= 1
                continue
            # 孤立的闭括号：当作普通文本
            result.append(char)
            continue
        if depth == 0:
            result.append(char)
    return "".join(result)


def cleanup_markdown(text: str) -> str:
    """去掉对 TTS 毫无意义的 Markdown 装饰符号。"""
    lines = []
    for line in text.split("\n"):
        line = _MD_LEAD_RE.sub("", line)
        line = _MD_RE.sub("", line)
        lines.append(line)
    return "\n".join(lines)


def normalize_text(text: str) -> str:
    """清理空白：去掉零宽字符、合并空格、删除空行。"""
    text = _INVISIBLE_RE.sub("", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for raw_line in text.split("\n"):
        line = _INLINE_SPACE_RE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def prepare_tts_text(raw_text: str, cleanup_md: bool = True) -> str:
    """把 AI 回复转换成「可以朗读的纯文本」。

    步骤：括号过滤 -> （可选）Markdown 清理 -> 空白整理。
    """
    text = strip_bracket_content(raw_text or "")
    if cleanup_md:
        text = cleanup_markdown(text)
    return normalize_text(text)


# ===========================================================================
# 小工具
# ===========================================================================


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


class TTSServerError(Exception):
    """TTS 服务返回的错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ===========================================================================
# 插件主体
# ===========================================================================


class VoiceReplyPlugin(Star):
    """拦截 AI 文本回复并替换为语音消息。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config or {}  # type: ignore[assignment]
        self._reload_config()

        self._semaphore: asyncio.Semaphore | None = None
        self._client: httpx.AsyncClient | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._enabled_override: bool | None = None  # /voice on|off 的运行时开关
        self._cache_dir: Path | None = None
        self._stats = {"attempt": 0, "success": 0, "failed": 0, "skipped": 0}

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def _section(self, name: str) -> dict[str, Any]:
        raw = (self.config or {}).get(name) if hasattr(self.config, "get") else None
        defaults = DEFAULT_CONFIG.get(name, {})
        merged = dict(defaults)
        if isinstance(raw, dict):
            merged.update({k: v for k, v in raw.items() if v is not None})
        return merged

    def _reload_config(self) -> None:
        server = self._section("tts_server")
        voice = self._section("voice")

        self.server_url = _as_str(server.get("url"), "http://127.0.0.1:8080").rstrip("/")
        self.api_key = _as_str(server.get("api_key"), "")
        self.delivery = _as_str(server.get("delivery"), "auto").lower()
        if self.delivery not in ("auto", "file", "url"):
            self.delivery = "auto"

        self.config_enabled = _as_bool(voice.get("enabled"), True)
        self.max_text_length = max(1, _as_int(voice.get("max_text_length"), 300))
        self.timeout = max(1.0, float(_as_int(voice.get("timeout"), 60)))
        self.max_concurrent = max(1, _as_int(voice.get("max_concurrent"), 2))
        self.keep_text = _as_bool(voice.get("keep_text"), False)
        self.only_llm_result = _as_bool(voice.get("only_llm_result"), False)
        self.cleanup_md = _as_bool(voice.get("cleanup_markdown"), True)
        self.cache_expire_minutes = max(1, _as_int(voice.get("cache_expire_minutes"), 60))

        raw_skip = voice.get("skip_platforms")
        if isinstance(raw_skip, str):
            raw_skip = [item.strip() for item in raw_skip.split(",") if item.strip()]
        elif isinstance(raw_skip, (list, tuple, set)):
            raw_skip = [str(item).strip() for item in raw_skip if str(item).strip()]
        else:
            raw_skip = list(DEFAULT_CONFIG["voice"]["skip_platforms"])
        self.skip_platforms = set(raw_skip)

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return self.config_enabled

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """插件被激活时调用。"""
        self._reload_config()
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            follow_redirects=True,
        )
        self._cache_dir = self._resolve_cache_dir()

        # 恢复上次的 /voice on|off 状态（AstrBot >= 4.9.2 支持插件 KV 存储）
        try:
            saved = await self.get_kv_data("enabled", None)
            if isinstance(saved, bool):
                self._enabled_override = saved
        except Exception as exc:  # pragma: no cover - 低版本 AstrBot 或存储不可用
            logger.debug("%s 读取持久化开关失败（忽略）: %s", LOG, exc)

        self._cleanup_task = asyncio.create_task(
            self._cache_cleanup_loop(),
            name=f"{PLUGIN_NAME}-cache-cleanup",
        )
        logger.info(
            "%s v%s 已加载 | 语音回复=%s | 服务=%s | 交付方式=%s | 最大长度=%d | "
            "并发=%d | 超时=%.0fs | API Key=%s",
            LOG,
            PLUGIN_VERSION,
            "开启" if self.enabled else "关闭",
            self.server_url,
            self.delivery,
            self.max_text_length,
            self.max_concurrent,
            self.timeout,
            "已配置" if self.api_key else "未配置",
        )

    async def terminate(self) -> None:
        """插件被停用/重载时调用。"""
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cleanup_task = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        logger.info("%s 已卸载。", LOG)

    def _resolve_cache_dir(self) -> Path:
        """本地音频缓存目录（优先 data/plugin_data/<plugin>）。"""
        try:
            from astrbot.api.star import StarTools

            base = StarTools.get_data_dir(PLUGIN_NAME)
            cache_dir = Path(base) / "audio"
        except Exception:
            cache_dir = Path(tempfile.gettempdir()) / PLUGIN_NAME / "audio"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    # ------------------------------------------------------------------
    # 核心钩子：发送消息前拦截
    # ------------------------------------------------------------------

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """AstrBot 官方「发送消息前」钩子：把文本结果装饰成语音结果。

        注意：本钩子只修改 ``event.get_result().chain``，不使用 yield。
        """
        try:
            await self._decorate(event)
        except Exception:
            # 任何意外都只记录日志：原始文本回复保持不动，正常发送
            logger.exception("%s 处理消息时出现未预期异常，已降级为文字回复", LOG)

    async def _decorate(self, event: AstrMessageEvent) -> None:
        if event.get_extra(FLAG_SKIP) or event.get_extra(FLAG_HANDLED):
            # 插件自身的指令回复 / 已处理过的事件，直接跳过（防重复处理）
            return
        if not self.enabled:
            logger.debug("%s 语音回复已关闭，跳过。", LOG)
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        # 流式输出（streaming）在 AstrBot 中是分片发送的：
        # 文本已经在流式过程中逐段发给了用户，这里不再做替换。
        content_type_name = getattr(getattr(result, "result_content_type", None), "name", "")
        if content_type_name == "STREAMING_RESULT":
            logger.debug("%s 流式输出片段，跳过语音转换。", LOG)
            return
        if content_type_name == "STREAMING_FINISH":
            logger.warning(
                "%s 检测到 AstrBot 流式输出：文本已分段发送，语音回复不会生效。"
                "如需语音回复，请关闭 AstrBot 的「流式输出（streaming_response）」。",
                LOG,
            )
            return

        if self.only_llm_result and not result.is_model_result():
            logger.debug("%s 非 LLM 结果，跳过。", LOG)
            return

        platform = ""
        try:
            platform = event.get_platform_name() or ""
        except Exception:
            pass
        if platform in self.skip_platforms:
            logger.debug("%s 平台 %s 不支持语音消息，保持文字回复。", LOG, platform)
            return

        plain_texts = [
            comp.text for comp in result.chain if isinstance(comp, Plain) and comp.text
        ]
        if not plain_texts:
            return

        raw_text = "\n".join(plain_texts)
        logger.info("%s 收到文本回复（%d 字）", LOG, len(raw_text))

        tts_text = prepare_tts_text(raw_text, self.cleanup_md)
        logger.info("%s 过滤括号内容后 TTS 文本长度：%d", LOG, len(tts_text))

        if not tts_text:
            # 例：整个回复只有 "[笑]" / "（开心）"
            self._stats["skipped"] += 1
            logger.info("%s 过滤后没有可朗读文本，正常发送原始文字。", LOG)
            return

        if len(tts_text) > self.max_text_length:
            self._stats["skipped"] += 1
            logger.info(
                "%s 文本长度 %d 超过上限 %d，不调用 TTS，直接发送文字。",
                LOG,
                len(tts_text),
                self.max_text_length,
            )
            return

        # 标记为已处理，避免同一事件被重复转换（防无限循环）
        event.set_extra(FLAG_HANDLED, True)
        self._stats["attempt"] += 1

        logger.info("%s 正在请求 TTS 服务：%s", LOG, self.server_url)
        audio = await self._synthesize(tts_text)
        if audio is None:
            self._stats["failed"] += 1
            logger.error("%s 已降级为文字回复。", LOG)
            return

        record = self._build_record(audio, tts_text)
        if record is None:
            self._stats["failed"] += 1
            logger.error("%s 音频类型无法识别，已降级为文字回复。", LOG)
            return

        self._replace_chain(result, record, raw_text)
        self._stats["success"] += 1
        logger.info("%s TTS 生成成功，正在发送语音：%s", LOG, audio)

    def _build_record(self, audio: Path | str, tts_text: str) -> Record | None:
        if isinstance(audio, Path):
            path_str = str(audio)
            return Record(file=path_str, url=path_str, path=path_str, text=tts_text)
        if isinstance(audio, str) and audio.startswith(("http://", "https://")):
            return Record(file=audio, url=audio, text=tts_text)
        return None

    def _replace_chain(self, result: Any, record: Record, raw_text: str) -> None:
        """把消息链里的文本段替换成语音段（保留图片等其它消息段与顺序）。"""
        new_chain: list[Any] = []
        inserted = False
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text and comp.text.strip():
                if not inserted:
                    new_chain.append(record)
                    inserted = True
                continue
            if isinstance(comp, Plain) and not comp.text.strip():
                continue
            new_chain.append(comp)
        if not inserted:  # 理论上不会发生，兜底
            new_chain = [record, *new_chain]
        if self.keep_text and raw_text.strip():
            # 可选：语音 + 原文一起发送（dual output）
            new_chain.append(Plain(raw_text.strip()))
            # 避免 AstrBot 的「文本转图片」把原文渲染成图片
            try:
                result.use_t2i_ = False  # type: ignore[attr-defined]
            except Exception:
                pass
        result.chain = new_chain

    # ------------------------------------------------------------------
    # TTS 调用
    # ------------------------------------------------------------------

    async def _synthesize(self, text: str) -> Path | str | None:
        """调用语音处理端。失败返回 None（由调用方降级为文字）。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
                follow_redirects=True,
            )
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)

        async def _worker() -> Path | str | None:
            async with self._semaphore:  # type: ignore[union-attr]
                if self.delivery == "url":
                    return await self._request_by_url(text)
                if self.delivery == "file":
                    return await self._request_by_file(text)
                # auto：优先本地文件，失败再退回 URL
                try:
                    return await self._request_by_file(text)
                except TTSServerError as exc:
                    logger.warning("%s 文件方式获取音频失败（%s），尝试 URL 方式。", LOG, exc.message)
                    return await self._request_by_url(text)

        try:
            return await asyncio.wait_for(_worker(), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.error("%s TTS 请求超时（%.0fs）", LOG, self.timeout)
            return None
        except TTSServerError as exc:
            logger.error("%s TTS 服务返回错误 [%s]: %s", LOG, exc.code, exc.message)
            return None
        except httpx.ConnectError as exc:
            logger.error("%s TTS 服务连接失败（%s）：%s", LOG, self.server_url, exc)
            return None
        except httpx.HTTPError as exc:
            logger.error("%s TTS 请求失败：%s", LOG, exc)
            return None
        except Exception as exc:
            logger.error("%s TTS 处理异常：%s", LOG, exc)
            return None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, audio/*"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        return headers

    @staticmethod
    def _parse_error(response: httpx.Response) -> TTSServerError:
        code = f"http_{response.status_code}"
        message = response.text[:200] if response.text else response.reason_phrase
        try:
            payload = response.json()
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
            elif isinstance(error, str):
                message = error
        except Exception:
            pass
        if response.status_code == 401:
            message = f"API Key 无效或缺失（{message}）"
        return TTSServerError(code, message)

    async def _request_by_file(self, text: str) -> Path:
        """向 /api/tts/file 请求音频并保存到本地缓存目录。"""
        assert self._client is not None
        if self._cache_dir is None:
            # 兜底：正常情况下 initialize() 已经准备好缓存目录
            self._cache_dir = self._resolve_cache_dir()

        response = await self._client.post(
            f"{self.server_url}/api/tts/file",
            json={"text": text},
            headers=self._headers(),
        )
        if response.status_code != 200:
            raise self._parse_error(response)

        content_type = response.headers.get("content-type", "")
        if "audio" not in content_type and not response.content[:4] in (b"RIFF", b"ID3\x03"):
            raise TTSServerError(
                "invalid_response",
                f"服务返回的响应不是音频（Content-Type: {content_type}）",
            )

        data = response.content
        if len(data) < 512:
            raise TTSServerError("empty_audio", "服务返回的音频数据为空。")

        suffix = ".mp3" if "mpeg" in content_type or data[:3] == b"ID3" else ".wav"
        digest = hashlib.sha256((text + suffix).encode("utf-8")).hexdigest()[:32]
        path = self._cache_dir / f"{digest}{suffix}"
        path.write_bytes(data)
        return path

    async def _request_by_url(self, text: str) -> str:
        """向 /api/tts 请求，并返回可直接交给消息平台的音频 URL。"""
        assert self._client is not None
        response = await self._client.post(
            f"{self.server_url}/api/tts",
            json={"text": text},
            headers=self._headers(),
        )
        if response.status_code != 200:
            raise self._parse_error(response)
        try:
            payload = response.json()
        except Exception as exc:
            raise TTSServerError("invalid_response", f"服务返回的不是合法 JSON: {exc}") from exc
        if not payload.get("success", False):
            error = payload.get("error") or {}
            raise TTSServerError(
                str(error.get("code", "unknown")),
                str(error.get("message", "未知错误")),
            )
        url = str(payload.get("audio_url") or "")
        if not url:
            raise TTSServerError("invalid_response", "服务未返回 audio_url。")
        if url.startswith("/"):
            url = f"{self.server_url}{url}"
        return url

    # ------------------------------------------------------------------
    # 管理指令
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("voice", alias={"语音"})
    async def voice_command(self, event: AstrMessageEvent, action: str = "status"):
        """语音回复管理指令：/voice on | off | status"""
        event.set_extra(FLAG_SKIP, True)  # 指令自己的回复不转语音
        action = _as_str(action, "status").lower()

        if action in ("on", "enable", "开启", "开"):
            await self._set_enabled(True)
            logger.info("%s 管理员 %s 开启语音回复", LOG, event.get_sender_id())
            yield event.plain_result(
                f"{LOG} 语音回复已开启 ✅\n"
                f"服务地址：{self.server_url}\n"
                f"最大文本长度：{self.max_text_length}",
            )
            return

        if action in ("off", "disable", "关闭", "关"):
            await self._set_enabled(False)
            logger.info("%s 管理员 %s 关闭语音回复", LOG, event.get_sender_id())
            yield event.plain_result(f"{LOG} 语音回复已关闭 ⛔（AI 回复将以文字发送）")
            return

        if action in ("status", "状态", "state", ""):
            yield event.plain_result(await self._status_text())
            return

        yield event.plain_result(
            f"{LOG} 用法：\n"
            "/voice on     开启语音回复\n"
            "/voice off    关闭语音回复\n"
            "/voice status 查看当前状态",
        )

    async def _set_enabled(self, value: bool) -> None:
        self._enabled_override = value
        try:
            await self.put_kv_data("enabled", value)
        except Exception as exc:  # pragma: no cover - 低版本 AstrBot
            logger.debug("%s 持久化开关失败（本次运行内仍然生效）: %s", LOG, exc)

    async def _status_text(self) -> str:
        lines = [
            f"{LOG} 语音回复状态",
            f"开关：{'开启 ✅' if self.enabled else '关闭 ⛔'}"
            f"（配置文件默认：{'开启' if self.config_enabled else '关闭'}）",
            f"服务地址：{self.server_url}",
            f"API Key：{'已配置' if self.api_key else '未配置'}",
            f"交付方式：{self.delivery}",
            f"最大文本长度：{self.max_text_length}",
            f"请求超时：{self.timeout:.0f}s，最大并发：{self.max_concurrent}",
            f"本次运行：尝试 {self._stats['attempt']} 次 / 成功 {self._stats['success']} 次 / "
            f"失败 {self._stats['failed']} 次 / 跳过 {self._stats['skipped']} 次",
        ]
        lines.append(f"服务健康检查：{await self._health_text()}")
        return "\n".join(lines)

    async def _health_text(self) -> str:
        if self._client is None:
            return "客户端未初始化"
        try:
            response = await self._client.get(
                f"{self.server_url}/api/health",
                timeout=min(5.0, self.timeout),
            )
            if response.status_code != 200:
                return f"异常（HTTP {response.status_code}）"
            payload = response.json()
            engine = payload.get("engine", {})
            storage = payload.get("storage", {}) or {}
            keep = storage.get("expire_minutes")
            keep_text = ""
            if isinstance(keep, (int, float)):
                keep_display = int(keep) if float(keep).is_integer() else round(float(keep), 2)
                keep_text = f" | 音频保留={keep_display}分钟"
            return (
                f"正常 | 状态={payload.get('status')} | "
                f"设备={engine.get('device')} | 模型={Path(str(engine.get('model', ''))).name or '-'} | "
                f"发音人={engine.get('speaker')} | 音调={engine.get('pitch')}{keep_text}"
            )
        except Exception as exc:
            return f"无法连接（{exc}）"

    # ------------------------------------------------------------------
    # 本地音频缓存清理
    # ------------------------------------------------------------------

    async def _cache_cleanup_loop(self) -> None:
        interval = max(60.0, self.cache_expire_minutes * 60 / 2)
        try:
            while True:
                await asyncio.sleep(interval)
                self._cleanup_cache_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            logger.exception("%s 缓存清理任务异常", LOG)

    def _cleanup_cache_once(self) -> int:
        if self._cache_dir is None or not self._cache_dir.exists():
            return 0
        deadline = time.time() - self.cache_expire_minutes * 60
        removed = 0
        for item in self._cache_dir.glob("*"):
            try:
                if item.is_file() and item.stat().st_mtime < deadline:
                    item.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info("%s 已清理 %d 个过期音频缓存文件。", LOG, removed)
        return removed
