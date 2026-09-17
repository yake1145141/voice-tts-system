"""配置加载与校验。

优先级：环境变量 > config.yaml > 内置默认值。
所有相对路径都相对于配置文件所在目录解析。
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        "public_base_url": "",
        "log_level": "INFO",
    },
    "security": {
        "api_key": "",
        "protect_audio": False,
    },
    "tts": {
        "source": "auto",
        "sapi_voice": "",
        "sapi_rate": 0,
        "sapi_timeout": 60,
        "espeak_voice": "cmn",     # espeak-ng 的中文音色（Linux/macOS 离线兜底）
        "espeak_speed": 180,
        "espeak_timeout": 60,
        "speaker": "zh-CN-YunxiNeural",
        "pitch": 5,
        "edge_pitch_hz": 0,
        "rate": 0,
        "volume": 0,
        "retries": 3,
    },
    "webui": {
        "enabled": True,
        "username": "admin",
        "password": "",          # 留空 = 网页控制台不校验密码
        "session_hours": 12,
    },
    "chunk": {
        "enabled": True,
        "max_chars": 80,         # 单段最大字数：RVC 显存峰值只与单段长度有关
        "min_chars": 10,         # 相邻过短片段合并阈值
    },
    "rvc": {
        "enabled": True,
        "model": "",
        "model_dir": "./models",
        "index": "",
        "index_rate": 0.75,
        "f0_method": "rmvpe",
        "device": "auto",
        # 2GB 显存（最低要求）也能用 GPU，靠 chunk 分段把峰值压到 2GB 以内
        "min_vram_mb": 1800,
        "allow_cpu_fallback": True,
        "is_half": True,
        "filter_radius": 3,
        "resample_sr": 0,
        "rms_mix_rate": 0.5,
        "protect": 0.33,
        "preload": True,
    },
    "storage": {
        "output_dir": "./output",
        "expire_minutes": 10,  # 生成后仅保留 10 分钟，到期自动删除
        "cleanup_interval_minutes": 2,
        "cache_by_text": True,
        "max_text_length": 2000,
    },
    "queue": {
        "max_concurrent": 2,
        "max_queue_size": 32,
        "timeout": 120,
    },
    "audio": {
        "output_format": "wav",
        "mp3_bitrate": "64k",
    },
}

SUPPORTED_F0_METHODS = ("rmvpe", "fcpe", "pm", "harvest", "dio", "crepe")
# edgetts = 微软在线语音；sapi = Windows 本地离线语音；espeak = espeak-ng 离线语音；
# auto = 先在线，失败自动转本地离线
SUPPORTED_TTS_SOURCES = ("edgetts", "sapi", "espeak", "auto")


class ConfigError(Exception):
    """配置文件错误。"""


def default_config_path() -> Path:
    """默认配置文件位置。

    - 环境变量 ``TTS_SERVER_CONFIG`` 优先；
    - 打包/冻结运行（PyInstaller 等）时取**可执行文件同目录**，方便把 config.yaml
      和 models/、output/ 一起放在 exe 旁边；
    - 源码运行取本文件所在目录（tts-server/）。
    """
    env = os.environ.get("TTS_SERVER_CONFIG")
    if env:
        return Path(env).expanduser()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.yaml"
    return Path(__file__).resolve().parent / "config.yaml"


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


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并配置，override 中的 None 会被忽略。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def pretty_number(value: float) -> float | int:
    """仅用于展示/接口输出：10.0 -> 10，10.5 -> 10.5。"""
    number = float(value)
    return int(number) if number.is_integer() else round(number, 2)


class Section(dict):
    """支持属性访问的配置节，读取时自动做类型转换。"""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - 正常情况下不会触发
            raise AttributeError(item) from exc

    def str(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        return default if value is None else str(value)

    def int(self, key: str, default: int = 0) -> int:
        return _as_int(self.get(key, default), default)

    def float(self, key: str, default: float = 0.0) -> float:
        return _as_float(self.get(key, default), default)

    def bool(self, key: str, default: bool = False) -> bool:
        return _as_bool(self.get(key, default), default)


class AppConfig:
    """整个服务的配置对象。"""

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self.data = data
        self.path = path
        self.base_dir = path.parent
        self.raw: dict[str, Any] = {}
        """用户配置文件里的原始内容（未与默认值合并），用于识别"是否显式配置过某项"。"""
        self.env_keys: set[tuple[str, str]] = set()
        """被环境变量覆盖过的 (section, key)。"""
        self.server = Section(data["server"])
        self.security = Section(data["security"])
        self.tts = Section(data["tts"])
        self.rvc = Section(data["rvc"])
        self.webui = Section(data["webui"])
        self.chunk = Section(data["chunk"])
        self.storage = Section(data["storage"])
        self.queue = Section(data["queue"])
        self.audio = Section(data["audio"])

    # ---------------- 加载 ----------------

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "AppConfig":
        config_path = Path(
            path or default_config_path()
        ).expanduser()
        if not config_path.is_absolute():
            config_path = (Path.cwd() / config_path).resolve()

        raw: dict[str, Any] = {}
        if config_path.exists():
            try:
                loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(f"配置文件 {config_path} 不是合法的 YAML: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ConfigError(f"配置文件 {config_path} 的顶层必须是键值对。")
            raw = loaded
        elif path is not None or os.environ.get("TTS_SERVER_CONFIG"):
            raise ConfigError(f"配置文件不存在: {config_path}")

        data = _deep_merge(DEFAULT_CONFIG, raw)
        env_keys = cls._apply_env_overrides(data)
        config = cls(data, config_path)
        config.raw = raw
        config.env_keys = env_keys
        config.normalize()
        config.validate()
        return config

    @staticmethod
    def _apply_env_overrides(data: dict[str, Any]) -> set[tuple[str, str]]:
        """用环境变量覆盖配置，返回被环境变量设置过的 (section, key) 集合。"""
        applied: set[tuple[str, str]] = set()
        env = os.environ
        mapping: list[tuple[str, str, str, type]] = [
            ("TTS_SERVER_HOST", "server", "host", str),
            ("TTS_SERVER_PORT", "server", "port", int),
            ("TTS_SERVER_PUBLIC_BASE_URL", "server", "public_base_url", str),
            ("TTS_SERVER_LOG_LEVEL", "server", "log_level", str),
            ("TTS_SERVER_API_KEY", "security", "api_key", str),
            ("TTS_SERVER_PROTECT_AUDIO", "security", "protect_audio", bool),
            ("RVC_ENABLED", "rvc", "enabled", bool),
            ("RVC_MODEL", "rvc", "model", str),
            ("RVC_MODEL_DIR", "rvc", "model_dir", str),
            ("RVC_INDEX", "rvc", "index", str),
            ("RVC_DEVICE", "rvc", "device", str),
            ("TTS_SERVER_OUTPUT_DIR", "storage", "output_dir", str),
            ("TTS_SERVER_EXPIRE_MINUTES", "storage", "expire_minutes", float),
            ("TTS_SERVER_MAX_CONCURRENT", "queue", "max_concurrent", int),
            ("TTS_SERVER_TIMEOUT", "queue", "timeout", float),
        ]
        for env_name, section, key, caster in mapping:
            value = env.get(env_name)
            if value is None or value == "":
                continue
            if caster is bool:
                data[section][key] = _as_bool(value, _as_bool(data[section].get(key)))
            elif caster is int:
                data[section][key] = _as_int(value, _as_int(data[section].get(key), 0))
            elif caster is float:
                data[section][key] = _as_float(value, _as_float(data[section].get(key), 0.0))
            else:
                data[section][key] = value
            applied.add((section, key))
        return applied

    # ---------------- 规范化 ----------------

    def normalize(self) -> None:
        raw_source = self.tts.str("source", "edgetts").strip().lower() or "edgetts"
        if raw_source in {"sapi", "sapi5", "windows"}:
            raw_source = "sapi"
        elif raw_source in {"espeak", "espeak-ng"}:
            raw_source = "espeak"
        elif raw_source in {"local", "offline", "builtin"}:
            # "本地语音"在 Windows 上是 SAPI，在其他系统上是 espeak-ng
            raw_source = "sapi" if os.name == "nt" else "espeak"
        elif raw_source in {"auto", "fallback"}:
            raw_source = "auto"
        self.source = raw_source
        self.tts["source"] = self.source
        self.speaker = self.tts.str("speaker", "zh-CN-YunxiNeural").strip()
        self.tts["speaker"] = self.speaker
        self.rvc_model_name = self.rvc.str("model", "").strip()
        self.rvc["model"] = self.rvc_model_name
        self.f0_method = self.rvc.str("f0_method", "rmvpe").strip().lower() or "rmvpe"
        self.rvc["f0_method"] = self.f0_method
        self.device = self.rvc.str("device", "auto").strip() or "auto"
        self.rvc["device"] = self.device
        self.output_format = self.audio.str("output_format", "wav").strip().lower() or "wav"
        self.audio["output_format"] = self.output_format
        self.api_key = self.security.str("api_key", "").strip()
        self.security["api_key"] = self.api_key

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)

    # ---------------- 路径 ----------------

    def resolve_path(self, value: str, *, relative_to: Path | None = None) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        return ((relative_to or self.base_dir) / path).resolve()

    @property
    def model_dir(self) -> Path:
        return self.resolve_path(self.rvc.str("model_dir", "./models"))

    @property
    def model_path(self) -> Path | None:
        """解析出最终的 .pth 模型路径（不存在时返回 None）。"""
        name = self.rvc_model_name
        if not name:
            return None
        candidate = Path(name).expanduser()
        if candidate.is_absolute():
            return candidate
        if len(candidate.parts) > 1:
            return self.resolve_path(name)
        if candidate.suffix == "":
            candidate = candidate.with_suffix(".pth")
        return (self.model_dir / candidate).resolve()

    @property
    def index_path(self) -> str:
        """返回 .index 路径字符串；未配置或不存在时返回空字符串。"""
        name = self.rvc.str("index", "").strip()
        if not name:
            return ""
        candidate = Path(name).expanduser()
        if candidate.is_absolute():
            path = candidate
        elif len(candidate.parts) > 1:
            path = self.resolve_path(name)
        else:
            path = (self.model_dir / candidate).resolve()
        return str(path) if path.exists() else ""

    @property
    def output_dir(self) -> Path:
        return self.resolve_path(self.storage.str("output_dir", "./output"))

    @property
    def expire_minutes(self) -> float:
        """音频保留时长（分钟）。默认 10 分钟，到期自动删除。

        兼容旧配置：若配置文件里仍写着 ``storage.expire_hours`` 且未显式配置
        ``storage.expire_minutes``，则按小时换算；环境变量优先级最高。
        """
        raw_storage = self.raw.get("storage") if isinstance(self.raw, dict) else None
        raw_storage = raw_storage if isinstance(raw_storage, dict) else {}
        env_override = ("storage", "expire_minutes") in self.env_keys
        if not env_override and "expire_minutes" not in raw_storage and "expire_hours" in raw_storage:
            return _as_float(raw_storage["expire_hours"], 10 / 60) * 60
        return _as_float(self.storage.get("expire_minutes"), 10.0)

    # ---------------- 校验 ----------------

    def validate(self) -> None:
        if self.source not in SUPPORTED_TTS_SOURCES:
            raise ConfigError(
                f"tts.source 只支持 {sorted(SUPPORTED_TTS_SOURCES)}，当前为 '{self.source}'。"
                "（edgetts = 在线微软语音；sapi = Windows 本地离线语音；auto = 在线失败自动转本地）",
            )
        # 'auto' 在非 Windows 上会自动退化成纯 edgetts，不算错误；只有显式指定 sapi 才报错
        if self.source == "sapi" and os.name != "nt":
            raise ConfigError(
                "tts.source = 'sapi' 需要 Windows 的 SAPI5 语音，当前系统不支持。"
                "Linux / Docker 请使用 'edgetts'、'espeak' 或 'auto'。",
            )
        if not self.speaker:
            raise ConfigError("tts.speaker 不能为空，例如 zh-CN-YunxiNeural。")

        if self.f0_method not in SUPPORTED_F0_METHODS:
            raise ConfigError(
                f"rvc.f0_method 必须是 {', '.join(SUPPORTED_F0_METHODS)} 之一，"
                f"当前为 '{self.f0_method}'。",
            )
        if self.output_format not in ("wav", "mp3"):
            raise ConfigError("audio.output_format 只支持 wav 或 mp3。")
        if self.device != "auto" and not (
            self.device == "cpu" or self.device.count(":") == 1
        ):
            raise ConfigError(
                f"rvc.device 格式错误: '{self.device}'，应为 auto / cpu / cuda:0 / mps:0。",
            )

        # RVC 开关与模型
        rvc_enabled = self.rvc.bool("enabled", True)
        if rvc_enabled:
            if not self.rvc_model_name:
                raise ConfigError(
                    "rvc.model 未配置。请在 config.yaml 中填写指定的 RVC 模型名称，"
                    '例如 rvc.model: "MyVoice.pth"。',
                )
            model_path = self.model_path
            if model_path is None or not model_path.exists():
                raise ConfigError(
                    f"找不到 RVC 模型文件: {model_path}。\n"
                    f"请把 .pth 模型放到 {self.model_dir} 目录，"
                    "或把 rvc.model 写成模型的绝对路径。",
                )
            if model_path.suffix.lower() != ".pth":
                raise ConfigError(f"RVC 模型必须是 .pth 文件，当前为 {model_path.name}。")

        if self.queue.int("max_concurrent", 2) < 1:
            raise ConfigError("queue.max_concurrent 必须 >= 1。")
        if self.queue.int("max_queue_size", 32) < 0:
            raise ConfigError("queue.max_queue_size 必须 >= 0。")
        if self.queue.float("timeout", 120) <= 0:
            raise ConfigError("queue.timeout 必须 > 0。")
        if self.expire_minutes <= 0:
            raise ConfigError("storage.expire_minutes 必须 > 0（单位：分钟）。")
        if self.storage.int("max_text_length", 2000) < 1:
            raise ConfigError("storage.max_text_length 必须 >= 1。")

    def describe(self) -> dict[str, Any]:
        """用于日志与 /api/health 的安全描述（不含密钥）。"""
        return {
            "config_file": str(self.path),
            "server": f"{self.server.str('host')}:{self.server.int('port')}",
            "auth_enabled": self.auth_enabled,
            "webui_protected": bool(self.webui.str("password", "").strip()),
            "chunk": {
                "enabled": self.chunk.bool("enabled", True),
                "max_chars": self.chunk.int("max_chars", 80),
            },
            "tts_source": self.source,
            "speaker": self.speaker,
            "pitch": self.tts.int("pitch", 0),
            "rvc_enabled": self.rvc.bool("enabled", True),
            "rvc_model": str(self.model_path) if self.model_path else "",
            "rvc_index": self.index_path,
            "f0_method": self.f0_method,
            "device": self.device,
            "output_dir": str(self.output_dir),
            "output_format": self.output_format,
            "expire_minutes": pretty_number(self.expire_minutes),
            "cleanup_interval_minutes": pretty_number(
                self.storage.float("cleanup_interval_minutes", 2),
            ),
            "max_concurrent": self.queue.int("max_concurrent", 2),
            "timeout": self.queue.float("timeout", 120),
        }
