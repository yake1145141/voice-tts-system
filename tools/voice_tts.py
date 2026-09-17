#!/usr/bin/env python3
"""语音服务调用客户端 —— 单文件、零依赖（只用 Python 标准库）。

把本文件复制到任何项目里即可调用 tts-with-rvc 语音处理端（Windows 整合包 /
Linux 便携包 / 安卓版 都是同一套 HTTP 接口）。

最简用法（3 行）：

    from voice_tts import VoiceTTS
    tts = VoiceTTS()                       # 自动从 config.yaml / 环境变量读地址与 Key
    path = tts.say("你好，这是一条语音消息。")   # 合成并保存，返回音频文件路径

常用写法：

    tts = VoiceTTS("http://127.0.0.1:8080", api_key="win-tts-key")
    tts.speak("直接播放这句话")              # 合成 + 播放
    data = tts.synthesize_bytes("只要字节流")   # 不落盘，拿到 wav bytes
    url  = tts.audio_url("给我一个可下载的链接")  # 交给别的平台去拉取
    tts.health()                            # 服务状态（dict）
    if tts.available(): ...                 # 服务是否可用（失败不抛异常）

命令行用法：

    python voice_tts.py "你好"                # 合成并保存
    python voice_tts.py "你好" --play         # 合成并播放
    python voice_tts.py --health              # 查看服务状态
    python voice_tts.py "你好" --url http://192.168.1.10:8080 --api-key xxx

地址与 API Key 的查找顺序：
    1) 构造参数 / 命令行参数
    2) 环境变量 VOICE_TTS_URL、VOICE_TTS_API_KEY
    3) 环境变量 TTS_SERVER_URL、TTS_SERVER_API_KEY
    4) 附近的 config.yaml（./config.yaml、./tts-server/config.yaml、上级目录同理）
    5) 默认 http://127.0.0.1:8080（无 Key）

依赖：无（urllib + json + wave 都是标准库）。Python 3.8+ 可用。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any

__all__ = ["VoiceTTS", "VoiceTTSError", "DEFAULT_URL"]
__version__ = "1.0.0"

DEFAULT_URL = "http://127.0.0.1:8080"
_CONFIG_NAMES = (
    Path("config.yaml"),
    Path("tts-server") / "config.yaml",
    Path("..") / "config.yaml",
    Path("..") / "tts-server" / "config.yaml",
)


class VoiceTTSError(Exception):
    """调用语音服务失败。``code`` 为服务端错误码或客户端错误标识。"""

    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.status = status


def _read_config_yaml(path: Path) -> dict[str, Any]:
    """从 config.yaml 里取 url / api_key（有 PyYAML 就用，没有就极简解析）。"""
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        import yaml

        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            server = data.get("server") or {}
            security = data.get("security") or {}
            host = str(server.get("host", "") or "")
            if host in ("0.0.0.0", "::", ""):
                host = "127.0.0.1"
            port = server.get("port")
            return {
                "url": f"http://{host}:{port}" if port else "",
                "api_key": str(security.get("api_key", "") or ""),
            }
    except Exception:
        pass

    import re

    def pick(key: str) -> str:
        match = re.search(rf"^\s*{key}\s*:\s*(.+?)\s*(?:#.*)?$", text, re.MULTILINE)
        return match.group(1).strip().strip("'\"") if match else ""

    host = pick("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = pick("port")
    return {
        "url": f"http://{host}:{port}" if port else "",
        "api_key": pick("api_key"),
    }


def _discover() -> tuple[str, str]:
    """返回 (url, api_key)。"""
    url = os.environ.get("VOICE_TTS_URL") or os.environ.get("TTS_SERVER_URL") or ""
    key = os.environ.get("VOICE_TTS_API_KEY") or os.environ.get("TTS_SERVER_API_KEY") or ""
    if url and key:
        return url, key

    seen: set[Path] = set()
    base = Path.cwd()
    for _ in range(3):  # 当前目录 + 上两级，覆盖 src/部署/包内 等常见布局
        for name in _CONFIG_NAMES:
            candidate = (base / name).resolve()
            if candidate in seen or not candidate.is_file():
                continue
            seen.add(candidate)
            info = _read_config_yaml(candidate)
            if info:
                url = url or info.get("url", "")
                key = key or info.get("api_key", "")
                if url and key:
                    return url, key
        base = base.parent
    return url or DEFAULT_URL, key


class VoiceTTS:
    """语音服务客户端。

    Args:
        base_url: 服务地址，例如 http://127.0.0.1:8080；留空则自动发现。
        api_key: 服务端 API Key；留空则自动发现（服务端未开鉴权时可为空）。
        timeout: 单次请求超时（秒）。合成较慢，默认 60 秒。
        retries: 网络抖动/服务 5xx 时的重试次数（默认 2 次，指数退避）。
        out_dir: 保存音频的目录，默认当前目录下的 tts_output/。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        retries: int = 2,
        out_dir: str | os.PathLike | None = None,
    ) -> None:
        found_url, found_key = ("", "")
        if not (base_url and api_key is not None):
            found_url, found_key = _discover()
        self.base_url = (base_url or found_url or DEFAULT_URL).rstrip("/")
        self.api_key = (api_key if api_key is not None else found_key) or ""
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.out_dir = Path(out_dir) if out_dir else Path.cwd() / "tts_output"

    # ------------------------------------------------------------------
    # 低层 HTTP
    # ------------------------------------------------------------------

    def _headers(self, json_body: bool = True) -> dict[str, str]:
        headers = {"Accept": "application/json, audio/*", "User-Agent": f"voice-tts/{__version__}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        return headers

    @staticmethod
    def _parse_error(exc: urllib.error.HTTPError, body: bytes) -> VoiceTTSError:
        code, message = f"http_{exc.code}", (exc.reason or "请求失败")
        try:
            payload = json.loads(body.decode("utf-8", errors="ignore"))
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
            elif error:
                message = str(error)
            elif payload.get("detail"):
                message = str(payload["detail"])
        except Exception:
            text = body.decode("utf-8", errors="ignore").strip()
            if text:
                message = text[:200]
        if exc.code == 401 and "api key" not in str(message).lower():
            message = f"API Key 无效或缺失（{message}）"
        return VoiceTTSError(code, str(message), exc.code)

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        method: str = "POST",
    ) -> tuple[bytes, dict[str, str]]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, data=data, method=method)
            for name, value in self._headers(json_body=data is not None).items():
                request.add_header(name, value)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    return response.read(), headers
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read()
                except Exception:
                    pass
                error = self._parse_error(exc, body)
                # 4xx 是请求本身的问题，重试没有意义
                if 400 <= exc.code < 500:
                    raise error from exc
                last_error = error
            except urllib.error.URLError as exc:
                last_error = VoiceTTSError(
                    "connect_error",
                    f"无法连接语音服务 {self.base_url}（{getattr(exc, 'reason', exc)}）",
                )
            except TimeoutError as exc:
                last_error = VoiceTTSError("client_timeout", f"请求超时（>{self.timeout:.0f}s）")
            except Exception as exc:  # noqa: BLE001
                last_error = VoiceTTSError("client_error", f"{type(exc).__name__}: {exc}")

            if attempt < self.retries:
                time.sleep(min(2 ** attempt * 0.5, 5.0))

        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------
    # 对外能力
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """获取服务状态（不需要 API Key）。"""
        body, _ = self._request("/api/health", None, method="GET")
        return json.loads(body.decode("utf-8"))

    def available(self) -> bool:
        """服务是否可用（不抛异常，适合做健康检查/降级判断）。"""
        try:
            return bool(self.health().get("success"))
        except Exception:
            return False

    def synthesize_bytes(self, text: str) -> bytes:
        """合成语音并直接返回 wav 字节流（不写磁盘）。"""
        text = (text or "").strip()
        if not text:
            raise VoiceTTSError("empty_text", "要合成的文本不能为空。")
        body, headers = self._request("/api/tts/file", {"text": text})
        if len(body) < 512:
            raise VoiceTTSError("empty_audio", "服务返回的音频为空。")
        if not headers.get("content-type", "").startswith("audio") and body[:4] != b"RIFF":
            raise VoiceTTSError(
                "invalid_response",
                f"服务返回的不是音频（Content-Type: {headers.get('content-type')}）",
            )
        return body

    def synthesize(
        self,
        text: str,
        dest: str | os.PathLike | None = None,
        *,
        filename: str | None = None,
        out_dir: str | os.PathLike | None = None,
    ) -> Path:
        """合成语音并保存，返回音频文件路径。

        Args:
            text: 要合成的文本。
            dest: 目标文件路径（优先级最高）。
            filename: 只给文件名时，保存到 out_dir。
            out_dir: 保存目录，默认构造时的 out_dir（默认 ./tts_output）。
        """
        path = self._resolve_target(dest, filename, out_dir)
        data = self.synthesize_bytes(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _default_filename(self) -> str:
        return f"tts-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.wav"

    def _resolve_target(
        self,
        dest: str | os.PathLike | None,
        filename: str | None,
        out_dir: str | os.PathLike | None,
    ) -> Path:
        """决定音频写到哪里：dest 既可以是文件路径，也可以是目录。"""
        default_dir = Path(out_dir) if out_dir else self.out_dir
        name = filename or self._default_filename()
        if not dest:
            return default_dir / name

        target = Path(dest)
        # 已存在的目录、以路径分隔符结尾、或没有扩展名 → 当成目录
        looks_like_dir = target.is_dir() or str(dest).endswith(("/", "\\")) or not target.suffix
        if looks_like_dir:
            return target / name
        return target

    # 语义化别名，看代码更直观
    say = synthesize

    def audio_url(self, text: str) -> str:
        """合成语音，返回可由其它平台直接拉取的音频 URL。"""
        text = (text or "").strip()
        if not text:
            raise VoiceTTSError("empty_text", "要合成的文本不能为空。")
        body, _ = self._request("/api/tts", {"text": text})
        payload = json.loads(body.decode("utf-8"))
        url = str(payload.get("audio_url") or "")
        if not url:
            raise VoiceTTSError("invalid_response", "服务未返回 audio_url。")
        if url.startswith("/"):
            url = f"{self.base_url}{url}"
        return url

    def speak(self, text: str, **kwargs: Any) -> Path:
        """合成语音并立即播放（失败时抛 VoiceTTSError）。"""
        path = self.synthesize(text, **kwargs)
        play_audio(path)
        return path

    def duration(self, path: str | os.PathLike) -> float | None:
        """读取 wav 时长（秒），用于估算是否超长；读取失败返回 None。"""
        try:
            with wave.open(str(path), "rb") as handle:
                return handle.getnframes() / float(handle.getframerate())
        except Exception:
            return None


def play_audio(path: str | os.PathLike) -> str:
    """尽量用系统自带播放器播放，返回实际使用的方式。"""
    file = Path(path)
    if os.name == "nt":
        if file.suffix.lower() == ".wav":
            try:
                import winsound

                winsound.PlaySound(str(file), winsound.SND_FILENAME)
                return "winsound"
            except Exception:
                pass
        if shutil.which("ffplay"):
            subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(file)])
            return "ffplay"
        os.startfile(str(file))  # type: ignore[attr-defined]
        return "默认播放器"

    for command, name in (
        (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(file)], "ffplay"),
        (["afplay", str(file)], "afplay"),
        (["aplay", "-q", str(file)], "aplay"),
        (["mpv", "--really-quiet", str(file)], "mpv"),
    ):
        if shutil.which(command[0]):
            subprocess.run(command)
            return name
    return "未找到播放器"


# ----------------------------------------------------------------------
# 命令行入口（可选，方便直接测试）
# ----------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="voice_tts.py",
        description="调用 tts-with-rvc 语音服务（单文件、零依赖）",
    )
    parser.add_argument("text", nargs="?", help="要合成的文本（省略则从标准输入读取）")
    parser.add_argument("--url", help="服务地址，默认自动发现")
    parser.add_argument("--api-key", help="API Key，默认自动发现")
    parser.add_argument("--out", help="保存路径（文件或目录）")
    parser.add_argument("--play", action="store_true", help="合成后播放")
    parser.add_argument("--json-url", action="store_true", help="只返回 audio_url（url 模式）")
    parser.add_argument("--health", action="store_true", help="查看服务状态后退出")
    parser.add_argument("--timeout", type=float, default=60.0, help="超时秒数（默认 60）")
    args = parser.parse_args(argv)

    client = VoiceTTS(args.url, args.api_key, timeout=args.timeout)

    try:
        if args.health:
            info = client.health()
            engine = info.get("engine", {})
            print(f"服务端      : {client.base_url}")
            print(f"状态        : {info.get('status')} | 引擎就绪: {engine.get('ready')}")
            print(
                f"设备        : {engine.get('device')} (fp16={engine.get('is_half')})"
                f" | 模型: {Path(str(engine.get('model', ''))).name or '-'}"
            )
            print(f"发音人      : {engine.get('speaker')} | 音调: {engine.get('pitch')} | f0: {engine.get('f0_method')}")
            print(f"鉴权        : {'已开启' if info.get('auth_enabled') else '未开启'}")
            return 0

        text = args.text or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
        if not text:
            parser.error("请提供要合成的文本，例如：python voice_tts.py \"你好\"")

        started = time.perf_counter()
        if args.json_url:
            print(client.audio_url(text))
            return 0

        # 先合成（只计时合成），需要播放时再单独播放，避免把播放时间算进耗时
        path = client.synthesize(text, dest=args.out)
        cost = time.perf_counter() - started
        duration = client.duration(path)
        print(f"已保存      : {path}（{path.stat().st_size / 1024:.1f} KB）")
        if duration:
            print(f"音频时长    : {duration:.2f}s | 耗时 {cost:.2f}s | 实时倍率 {cost / duration:.2f}x")
        if args.play:
            print(f"播放        : {play_audio(path)}")
        return 0
    except VoiceTTSError as exc:
        print(f"❌ 调用失败 [{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.code == "connect_error":
            print("   服务没启动？双击整合包里的「启动语音服务.bat」，或检查 --url 是否正确。", file=sys.stderr)
        elif exc.code == "unauthorized":
            print("   API Key 不一致：请让 --api-key 与服务端 config.yaml 的 security.api_key 相同。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(_main())
