#!/usr/bin/env python3
"""本地测试客户端：直接调用 tts-server（tts-with-rvc 语音处理端）。

只用 Python 标准库，不依赖 AstrBot，也不需要安装任何第三方包。

常用命令：
    python tools/tts_client.py health                     # 服务健康检查
    python tools/tts_client.py say "你好，这是语音测试。"   # 合成一条并保存
    python tools/tts_client.py say "你好" --play          # 合成并播放
    python tools/tts_client.py batch texts.txt            # 逐行批量合成
    python tools/tts_client.py bench --count 8 -c 2       # 并发压测
    python tools/tts_client.py filter "你好呀！（开心地笑）" # 预览插件会发给 TTS 的文本

地址与 API Key 会自动识别，优先级：
    --url / --api-key  >  环境变量 TTS_SERVER_URL / TTS_SERVER_API_KEY
    >  tts-server/config.yaml  >  默认 http://127.0.0.1:8080
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from contextlib import closing
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_TIMEOUT = 180.0
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 源码布局：<root>/tts-server/config.yaml；便携包布局：<bundle>/config.yaml
CONFIG_CANDIDATES = [
    PROJECT_ROOT / "tts-server" / "config.yaml",
    PROJECT_ROOT / "config.yaml",
]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "client_output"


# ===========================================================================
# 地址 / API Key 自动识别
# ===========================================================================


def read_server_config() -> dict:
    """读取服务端配置里的 host/port/api_key（只读，不做依赖检查）。

    依次尝试：源码布局的 tts-server/config.yaml、便携包布局的 ./config.yaml。
    """
    for config_path in CONFIG_CANDIDATES:
        if not config_path.exists():
            continue
        text = config_path.read_text(encoding="utf-8", errors="ignore")
        try:  # 有 PyYAML 就用它
            import yaml

            data = yaml.safe_load(text) or {}
            if isinstance(data, dict) and ("server" in data or "security" in data):
                server = data.get("server") or {}
                security = data.get("security") or {}
                return {
                    "host": str(server.get("host", "")),
                    "port": server.get("port"),
                    "api_key": str(security.get("api_key", "") or ""),
                }
        except Exception:
            pass  # 没有 PyYAML 时用下面的极简解析

        def pick(key: str) -> str:
            match = re.search(rf"^\s*{key}\s*:\s*(.+?)\s*(?:#.*)?$", text, re.MULTILINE)
            return match.group(1).strip().strip("'\"") if match else ""

        return {
            "host": pick("host"),
            "port": pick("port"),
            "api_key": pick("api_key"),
        }
    return {}


def resolve_endpoint(args: argparse.Namespace) -> tuple[str, str]:
    """返回 (base_url, api_key)。"""
    config = read_server_config()

    url = args.url or os.environ.get("TTS_SERVER_URL") or ""
    if not url and config.get("port"):
        host = config.get("host") or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        url = f"http://{host}:{config['port']}"
    url = (url or DEFAULT_URL).rstrip("/")

    api_key = args.api_key or os.environ.get("TTS_SERVER_API_KEY") or config.get("api_key") or ""
    return url, api_key


# ===========================================================================
# HTTP
# ===========================================================================


class ClientError(Exception):
    """带服务端错误码的异常。"""

    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _headers(api_key: str, json_body: bool = True) -> dict[str, str]:
    headers = {"Accept": "application/json, audio/*", "User-Agent": "tts-client/1.0"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-Key"] = api_key
    return headers


def _decode_error(exc: urllib.error.HTTPError) -> ClientError:
    body = b""
    try:
        body = exc.read()
    except Exception:
        pass
    code, message = f"http_{exc.code}", exc.reason or ""
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
        message = message or body.decode("utf-8", errors="ignore")[:200]
    return ClientError(code, message or "请求失败", exc.code)


def http_request(
    url: str,
    api_key: str,
    payload: dict | None = None,
    *,
    method: str = "POST",
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bytes, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in _headers(api_key, json_body=payload is not None).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # 统一小写，避免服务端/代理对响应头大小写的不同处理
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.read(), headers
    except urllib.error.HTTPError as exc:
        raise _decode_error(exc) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ClientError("connect_error", f"无法连接服务端 {url}（{reason}）") from exc
    except TimeoutError as exc:
        raise ClientError("client_timeout", f"客户端等待超时（>{timeout:.0f}s）") from exc


def api_health(base_url: str, api_key: str, timeout: float) -> dict:
    body, _ = http_request(f"{base_url}/api/health", api_key, method="GET", timeout=timeout)
    return json.loads(body.decode("utf-8"))


def api_tts(
    base_url: str,
    api_key: str,
    text: str,
    mode: str = "file",
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bytes | None, dict, dict, float]:
    """调用 TTS 接口。

    mode="file": 直接拿音频二进制；mode="url"/"json": 拿 JSON（含 audio_url）。
    返回 (音频字节或 None, 响应头, JSON 结果, 耗时秒)。
    """
    started = time.perf_counter()
    if mode == "file":
        audio, headers = http_request(
            f"{base_url}/api/tts/file",
            api_key,
            {"text": text},
            timeout=timeout,
        )
        return audio, headers, {}, time.perf_counter() - started

    body, headers = http_request(f"{base_url}/api/tts", api_key, {"text": text}, timeout=timeout)
    payload = json.loads(body.decode("utf-8"))
    if not payload.get("success"):
        error = payload.get("error") or {}
        raise ClientError(str(error.get("code", "unknown")), str(error.get("message", "未知错误")))
    return None, headers, payload, time.perf_counter() - started


# ===========================================================================
# 音频工具
# ===========================================================================


def wav_duration(path: Path) -> float | None:
    try:
        with closing(wave.open(str(path), "rb")) as handle:
            return handle.getnframes() / float(handle.getframerate())
    except Exception:
        return None


def audio_duration_from_bytes(data: bytes, suffix: str) -> float | None:
    """估算音频时长：wav 直接读头，mp3 用 ffprobe（有的话）。"""
    if suffix == ".wav":
        try:
            import io

            with closing(wave.open(io.BytesIO(data), "rb")) as handle:
                return handle.getnframes() / float(handle.getframerate())
        except Exception:
            return None
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    tmp = Path(os.environ.get("TEMP", "/tmp")) / f"tts-client-probe{suffix}"
    try:
        tmp.write_bytes(data)
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(tmp)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(out.stdout.strip()) if out.stdout.strip() else None
    except Exception:
        return None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def play_audio(path: Path) -> str:
    """尽量用系统自带播放器播放，返回实际使用的方式。"""
    suffix = path.suffix.lower()
    if os.name == "nt":
        if suffix == ".wav":
            try:
                import winsound

                winsound.PlaySound(str(path), winsound.SND_FILENAME)
                return "winsound"
            except Exception:
                pass
        if shutil.which("ffplay"):
            subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)])
            return "ffplay"
        os.startfile(str(path))  # type: ignore[attr-defined]  # 用默认程序打开
        return "默认播放器"

    for cmd, name in (
        (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)], "ffplay"),
        (["afplay", str(path)], "afplay"),
        (["aplay", "-q", str(path)], "aplay"),
        (["mpv", "--really-quiet", str(path)], "mpv"),
    ):
        if shutil.which(cmd[0]):
            subprocess.run(cmd)
            return name
    return "未找到播放器"


# ===========================================================================
# 文本处理预览（与插件 astrbot_plugin_voice_reply/main.py 保持同一算法）
# ===========================================================================

BRACKET_PAIRS = {"（": "）", "(": ")", "[": "]", "【": "】"}
BRACKET_CLOSERS = set(BRACKET_PAIRS.values())
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_INLINE_SPACE_RE = re.compile(r"[ \t\u3000]+")
_MD_RE = re.compile(r"[*_~`]+")
_MD_LEAD_RE = re.compile(r"^\s*(?:#{1,6}\s*|>\s*|[-+]\s+|\d+[.、)]\s+)+")


def strip_bracket_content(text: str) -> str:
    """删除括号及其内部内容（支持嵌套），算法与插件完全一致。"""
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
            result.append(char)
            continue
        if depth == 0:
            result.append(char)
    return "".join(result)


def cleanup_markdown(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        line = _MD_LEAD_RE.sub("", line)
        line = _MD_RE.sub("", line)
        lines.append(line)
    return "\n".join(lines)


def normalize_text(text: str) -> str:
    text = _INVISIBLE_RE.sub("", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for raw_line in text.split("\n"):
        line = _INLINE_SPACE_RE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def prepare_tts_text(raw_text: str, cleanup_md: bool = True) -> str:
    """AI 回复 -> 可朗读文本（插件的同一套处理）。"""
    text = strip_bracket_content(raw_text or "")
    if cleanup_md:
        text = cleanup_markdown(text)
    return normalize_text(text)


# ===========================================================================
# 子命令实现
# ===========================================================================


def _read_input_text(text: str | None) -> str:
    """文本来自参数；没给就从标准输入读（支持管道）。"""
    if text:
        return text
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def _save_audio(data: bytes, suffix: str, output_dir: Path, name: str | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if name:
        path = output_dir / name
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = output_dir / f"tts-{stamp}-{int(time.time() * 1000) % 1000:03d}{suffix}"
    path.write_bytes(data)
    return path


def cmd_health(args: argparse.Namespace, base_url: str, api_key: str) -> int:
    data = api_health(base_url, api_key, args.timeout)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    engine = data.get("engine", {})
    storage = data.get("storage", {})
    stats = engine.get("stats", {})
    model = Path(str(engine.get("model", ""))).name or "-"
    print(f"服务端      : {base_url}")
    print(f"状态        : {data.get('status')} | 引擎就绪: {engine.get('ready')}")
    print(f"设备        : {engine.get('device')} (fp16={engine.get('is_half')}) | RVC: {engine.get('rvc_enabled')}")
    print(f"模型        : {model}")
    print(f"发音人      : {engine.get('speaker')} | 音调: {engine.get('pitch')} | f0: {engine.get('f0_method')}")
    print(
        f"并发        : {engine.get('max_concurrent')} "
        f"(进行中 {engine.get('inflight')} / 排队 {engine.get('queued')}) | 单任务超时 {engine.get('timeout')}s"
    )
    print(
        f"音频保留    : {storage.get('expire_minutes')} 分钟"
        f" | 目录现有 {storage.get('files')} 个文件 / {storage.get('total_mb')} MB"
    )
    print(f"鉴权        : {'已开启' if data.get('auth_enabled') else '未开启'}"
          f" | 本次请求带 Key: {'是' if api_key else '否'}")
    print(
        f"运行统计    : 推理 {stats.get('total')} 次 | 成功 {stats.get('success')} | 失败 {stats.get('failed')}"
        f" | 超时 {stats.get('timeout')} | 缓存命中 {stats.get('cache_hit')} 次（不占推理）"
        f" | 最近耗时 {stats.get('last_duration')}s"
    )
    if engine.get("last_error"):
        print(f"最近错误    : {engine.get('last_error')}")
    return 0


def cmd_say(args: argparse.Namespace, base_url: str, api_key: str) -> int:
    text = _read_input_text(args.text)
    if not text:
        print("请提供要合成的文本，例如：tts_client.py say \"你好\"", file=sys.stderr)
        return 2

    filtered = prepare_tts_text(text)
    print(f"发送文本    : {filtered!r}" if filtered != text else f"发送文本    : {text!r}")

    mode = args.mode
    audio, headers, payload, elapsed = api_tts(base_url, api_key, filtered, mode, args.timeout)

    result: dict = {"success": True, "mode": mode, "elapsed": round(elapsed, 3), "text": filtered}
    if mode == "file":
        suffix = ".mp3" if "mpeg" in headers.get("content-type", "") else ".wav"
        path = _save_audio(audio or b"", suffix, args.output_dir, args.out)
        duration = audio_duration_from_bytes(audio or b"", suffix)
        result.update({"file": str(path), "bytes": len(audio or b""), "duration": duration})
        print(f"保存文件    : {path}（{len(audio or b'') / 1024:.1f} KB）")
        if duration:
            print(f"音频时长    : {duration:.2f}s | 实时倍率 {elapsed / duration:.2f}x")
        print(f"耗时        : {elapsed:.2f}s | 缓存命中: {headers.get('x-audio-cached', '?')}")
        if args.play:
            print(f"播放        : {play_audio(path)}")
    else:
        audio_url = str(payload.get("audio_url", ""))
        result.update(payload)
        print(f"audio_url   : {audio_url}")
        print(f"耗时        : {elapsed:.2f}s | 格式 {payload.get('format')} | {payload.get('size_bytes')} 字节")
        if args.download:
            path_part = str(payload.get("audio_path") or "")
            if not path_part:
                path_part = audio_url[len(base_url):] if audio_url.startswith(base_url) else ""
            if path_part.startswith("/"):
                data, _ = http_request(f"{base_url}{path_part}", api_key, method="GET", timeout=args.timeout)
                suffix = f".{payload.get('format', 'wav')}"
                path = _save_audio(data, suffix, args.output_dir, args.out)
                duration = audio_duration_from_bytes(data, suffix)
                print(f"下载成功    : {path}（{len(data) / 1024:.1f} KB）"
                      + (f" | 音频时长 {duration:.2f}s" if duration else ""))
                result["file"] = str(path)
                if args.play:
                    print(f"播放        : {play_audio(path)}")
            else:
                print("提示        : 响应里没有可下载的路径，跳过下载。")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _load_texts(source: str) -> list[str]:
    if source == "-" or not source:
        raw = sys.stdin.read()
    else:
        path = Path(source)
        if not path.is_file():
            raise ClientError("file_not_found", f"文本文件不存在: {path}")
        raw = path.read_text(encoding="utf-8")
    texts = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            texts.append(line)
    return texts


def cmd_batch(args: argparse.Namespace, base_url: str, api_key: str) -> int:
    texts = _load_texts(args.file)
    if not texts:
        print("没有可合成的文本（文件为空或只有注释）。", file=sys.stderr)
        return 2

    print(f"共 {len(texts)} 条文本，并发 {args.concurrency}，模式 {args.mode}\n")
    started = time.perf_counter()
    rows: list[tuple[int, str, str, float, float | None, str]] = []

    def worker(index: int, raw: str):
        filtered = prepare_tts_text(raw)
        t0 = time.perf_counter()
        try:
            audio, headers, payload, elapsed = api_tts(base_url, api_key, filtered, args.mode, args.timeout)
            saved = ""
            duration = None
            if args.mode == "file":
                suffix = ".mp3" if "mpeg" in headers.get("content-type", "") else ".wav"
                path = _save_audio(audio or b"", suffix, args.output_dir)
                saved = path.name
                duration = audio_duration_from_bytes(audio or b"", suffix)
            else:
                saved = str(payload.get("filename", ""))
            return index, filtered, "ok", elapsed, duration, saved
        except ClientError as exc:
            return index, filtered, f"{exc.code}", time.perf_counter() - t0, None, exc.message[:60]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(worker, i, text) for i, text in enumerate(texts, 1)]
        for future in concurrent.futures.as_completed(futures):
            rows.append(future.result())

    rows.sort(key=lambda item: item[0])
    ok = 0
    for index, text, status, elapsed, duration, extra in rows:
        mark = "✅" if status == "ok" else "❌"
        if status == "ok":
            ok += 1
        rtf = f"{elapsed / duration:.2f}x" if duration else "-"
        print(f"{mark} #{index:<3} {elapsed:6.2f}s  RTF {rtf:>7}  {text[:28]:<30} {extra}")

    total = time.perf_counter() - started
    print(f"\n完成：成功 {ok}/{len(rows)}，总耗时 {total:.2f}s")
    if rows:
        durations = [item[3] for item in rows if item[2] == "ok"]
        if durations:
            print(f"延迟：平均 {statistics.mean(durations):.2f}s | "
                  f"最快 {min(durations):.2f}s | 最慢 {max(durations):.2f}s")
    if args.output_dir.exists():
        print(f"音频目录：{args.output_dir}")
    return 0 if ok == len(rows) else 1


BENCH_TEXTS = [
    "你好呀，很高兴认识你。",
    "今天天气不错，适合出去走走。",
    "这是一次并发压测，请忽略这条消息。",
    "语音合成的延迟取决于文本长度和当前负载。",
    "服务器会同时处理多个会话的请求。",
    "如果排队过长，请求会自动降级为文字回复。",
]


def cmd_bench(args: argparse.Namespace, base_url: str, api_key: str) -> int:
    pool_texts = [args.text] if args.text else list(BENCH_TEXTS)
    print(
        f"压测参数：请求 {args.count} 次 | 并发 {args.concurrency} | 预热 {args.warmup} 次 | 模式 {args.mode}"
        f" | 文本源：{'指定文本' if args.text else '内置句子池'}"
        + ("（--unique：强制绕过缓存）" if args.unique else "")
    )

    def one(index: int) -> tuple[str, float, float | None, str, float]:
        raw = pool_texts[index % len(pool_texts)]
        text = prepare_tts_text(raw)
        if args.unique:
            # 先过滤再补零宽空格（过滤器会删掉零宽字符，服务端不会）：
            # 语音内容不变，但服务端缓存 key 不同，可以测到真实的推理耗时
            text = text + "\u200b" * (index // len(pool_texts) + 1)
        t0 = time.perf_counter()
        try:
            audio, headers, payload, elapsed = api_tts(base_url, api_key, text, args.mode, args.timeout)
            duration = None
            if args.mode == "file":
                suffix = ".mp3" if "mpeg" in headers.get("content-type", "") else ".wav"
                duration = audio_duration_from_bytes(audio or b"", suffix)
                if args.keep:
                    _save_audio(audio or b"", suffix, args.output_dir, f"bench-{index:03d}{suffix}")
            return "ok", elapsed, duration, headers.get("x-audio-cached", "?"), time.perf_counter() - t0
        except ClientError as exc:
            return f"{exc.code}: {exc.message[:40]}", time.perf_counter() - t0, None, "?", time.perf_counter() - t0

    for i in range(args.warmup):
        status, elapsed, duration, cached, _ = one(i)
        print(f"预热 #{i + 1}: {status} {elapsed:.2f}s" + (f" | 音频 {duration:.2f}s" if duration else ""))

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        results = list(pool.map(one, range(args.count)))
    wall = time.perf_counter() - started

    ok_results = [item for item in results if item[0] == "ok"]
    failures: dict[str, int] = {}
    for status, *_ in results:
        if status != "ok":
            key = status.split(":")[0]
            failures[key] = failures.get(key, 0) + 1

    latencies = sorted(item[1] for item in ok_results)
    durations = [item[2] for item in ok_results if item[2]]
    cached_hits = sum(1 for item in ok_results if item[3] == "1")
    audio_total = sum(durations)

    print(f"\n成功 {len(ok_results)}/{args.count} | 总耗时 {wall:.2f}s | 吞吐 {len(ok_results) / wall:.2f} 条/秒"
          f" | 缓存命中 {cached_hits}")
    if failures:
        print("失败明细：" + "，".join(f"{code} × {count}" for code, count in failures.items()))
    if latencies:
        def pct(p: float) -> float:
            idx = min(len(latencies) - 1, int(len(latencies) * p))
            return latencies[idx]

        print(f"延迟(秒)：平均 {statistics.mean(latencies):.2f} | p50 {pct(0.5):.2f} | "
              f"p90 {pct(0.9):.2f} | 最大 {max(latencies):.2f}")
    if durations:
        print(f"音频总时长 {audio_total:.2f}s | 平均实时倍率 {(sum(latencies) / audio_total):.2f}x"
              f"（1 表示与音频等长）")
    if args.keep and args.output_dir.exists():
        print(f"音频目录：{args.output_dir}")
    if args.json:
        print(json.dumps(
            {
                "requests": args.count,
                "concurrency": args.concurrency,
                "success": len(ok_results),
                "failures": failures,
                "wall": round(wall, 3),
                "throughput": round(len(ok_results) / wall, 3) if wall else 0,
                "latency_avg": round(statistics.mean(latencies), 3) if latencies else None,
                "latency_max": round(max(latencies), 3) if latencies else None,
                "cache_hits": cached_hits,
            },
            ensure_ascii=False,
            indent=2,
        ))
    return 0 if len(ok_results) == args.count else 1


def cmd_filter(args: argparse.Namespace, base_url: str, api_key: str) -> int:
    del base_url, api_key
    text = _read_input_text(args.text)
    if not text:
        print("请提供要预览的文本。", file=sys.stderr)
        return 2

    filtered = prepare_tts_text(text)
    print(f"原始文本    : {text!r}")
    print(f"过滤后文本  : {filtered!r}")
    print(f"长度        : {len(filtered)} 字（上限 {args.max_length}）")
    if not filtered:
        verdict = "过滤后没有可朗读文本 → 不调用 TTS，直接发送原文"
    elif len(filtered) > args.max_length:
        verdict = "超过最大长度 → 不调用 TTS，直接发送原文"
    else:
        verdict = "会调用 TTS 转成语音"
    print(f"判定        : {verdict}")
    print(f"请求体      : {json.dumps({'text': filtered}, ensure_ascii=False)}")
    return 0


# ===========================================================================
# 命令行入口
# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tts_client.py",
        description="tts-with-rvc 语音处理端本地测试客户端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tools/tts_client.py health\n"
            "  python tools/tts_client.py say \"你好，这是语音测试。\" --play\n"
            "  python tools/tts_client.py batch texts.txt --concurrency 2\n"
            "  python tools/tts_client.py bench --count 10 --concurrency 2 --unique\n"
            "  python tools/tts_client.py filter \"你好呀！（开心地笑）\"\n"
        ),
    )
    parser.add_argument("--url", help="服务端地址，例如 http://127.0.0.1:8080")
    parser.add_argument("--api-key", help="服务端 API Key（默认读取环境变量或 tts-server/config.yaml）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单次请求超时秒数（默认 180）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果（便于脚本处理）")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="检查服务端状态")

    say = sub.add_parser("say", help="合成一条文本")
    say.add_argument("text", nargs="?", help="要合成的文本（省略则从标准输入读取）")
    say.add_argument("--mode", choices=["file", "url"], default="file",
                     help="file=直接下载音频（默认）；url=走 JSON 接口拿 audio_url")
    say.add_argument("--out", help="保存文件名（默认自动生成）")
    say.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="音频保存目录")
    say.add_argument("--play", action="store_true", help="合成后用系统播放器播放")
    say.add_argument("--download", action="store_true", default=True,
                     help="url 模式下顺带把音频下载下来验证（默认开启）")

    batch = sub.add_parser("batch", help="从文件逐行读取并批量合成")
    batch.add_argument("file", help="文本文件路径，- 表示从标准输入读取")
    batch.add_argument("--concurrency", "-c", type=int, default=1, help="并发数（默认 1）")
    batch.add_argument("--mode", choices=["file", "url"], default="file")
    batch.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)

    bench = sub.add_parser("bench", help="并发压测")
    bench.add_argument("--count", "-n", type=int, default=6, help="请求数量（默认 6）")
    bench.add_argument("--concurrency", "-c", type=int, default=1, help="并发数（默认 1）")
    bench.add_argument("--text", help="指定文本；不填则使用内置句子池轮换")
    bench.add_argument("--unique", action="store_true", help="强制绕过服务端文本缓存（更接近真实负载）")
    bench.add_argument("--warmup", type=int, default=1, help="预热次数（默认 1）")
    bench.add_argument("--keep", action="store_true", help="保留压测生成的音频")
    bench.add_argument("--mode", choices=["file", "url"], default="file")
    bench.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)

    filt = sub.add_parser("filter", help="预览插件会发送给 TTS 的文本（括号过滤）")
    filt.add_argument("text", nargs="?", help="原始 AI 回复（省略则从标准输入读取）")
    filt.add_argument("--max-length", type=int, default=300, help="与插件 voice.max_text_length 一致（默认 300）")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    base_url, api_key = resolve_endpoint(args)

    handlers = {
        "health": cmd_health,
        "say": cmd_say,
        "batch": cmd_batch,
        "bench": cmd_bench,
        "filter": cmd_filter,
    }
    try:
        return handlers[args.command](args, base_url, api_key)
    except ClientError as exc:
        print(f"❌ 请求失败 [{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.code == "connect_error":
            print("   服务没启动？先在 tts-server 目录执行：python main.py", file=sys.stderr)
        elif exc.code == "unauthorized":
            print("   API Key 不一致：请让 --api-key 与 tts-server/config.yaml 的 security.api_key 相同。",
                  file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
