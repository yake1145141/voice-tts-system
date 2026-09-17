"""长文本分段自测：切分规则、无内容丢失、分段后拼接、语音源回退顺序。

    python tests/test_chunking.py

不需要 GPU / RVC / 网络；只在验证音频拼接时用到 ffmpeg（没有就跳过那一项）。
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "tts-server"
sys.path.insert(0, str(SERVER_DIR))

from config import AppConfig  # noqa: E402
from engine import TTSEngine, split_text_for_chunks  # noqa: E402
from storage import AudioStorage  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  [PASS] {message}")
    else:
        print(f"  [FAIL] {message}")
        FAILURES.append(message)


def make_config(tmp: Path, *, max_chars: int = 80, source: str = "edgetts") -> Path:
    path = tmp / "config.yaml"
    path.write_text(
        "\n".join(
            [
                "server:",
                '  host: "127.0.0.1"',
                "  port: 18082",
                '  log_level: "WARNING"',
                "security:",
                '  api_key: ""',
                "tts:",
                f'  source: "{source}"',
                '  speaker: "zh-CN-YunxiNeural"',
                "rvc:",
                "  enabled: true",
                '  model: "MyVoice.pth"',
                '  model_dir: "./models"',
                '  device: "cpu"',
                "  preload: false",
                "chunk:",
                "  enabled: true",
                f"  max_chars: {max_chars}",
                "  min_chars: 10",
                "storage:",
                f"  output_dir: '{(tmp / 'output').as_posix()}'",
                "queue:",
                "  max_concurrent: 1",
                "  timeout: 60",
            ],
        ),
        encoding="utf-8",
    )
    return path


def test_split_rules() -> None:
    print("切分规则")
    text = "第一句话。第二句话！第三句话？"
    parts = split_text_for_chunks(text, 6, 2)
    check(parts == ["第一句话。", "第二句话！", "第三句话？"],
          f"按句末标点切分（实际 {parts}）")

    long_sentence = "这是一个很长的句子，里面有逗号可以断开，所以应该按逗号切分。"
    parts = split_text_for_chunks(long_sentence, 12, 4)
    check(all(len(p) <= 12 for p in parts), f"每段都不超过上限（实际 {[len(p) for p in parts]}）")
    check("".join(parts) == long_sentence, "切分后拼回去与原文完全一致")

    parts = split_text_for_chunks("a" * 25, 10, 2)
    check(parts == ["a" * 10, "a" * 10, "a" * 5], f"无标点时按长度硬切（实际 {[len(p) for p in parts]}）")

    parts = split_text_for_chunks("你好。", 80, 10)
    check(parts == ["你好。"], "短文本不切分")

    parts = split_text_for_chunks("", 80, 10)
    check(parts == [], "空文本返回空列表")

    # 纯标点碎片不能单独成段，否则会生成一段无声音频
    parts = split_text_for_chunks(long_sentence, 15, 4)
    check(all(any(ch.isalnum() for ch in p) for p in parts),
          f"没有纯标点碎片（实际 {parts}）")


def test_no_content_loss() -> None:
    print("随机长度下不丢内容")
    samples = [
        "你好呀，今天天气不错。（开心地笑）我们出去玩吧！",
        "Hello world! 这是一段中英混合的文本，用来测试切分；标点符号【括号】也要保留。",
        "很长很长的一句话" * 20,
        "短。",
    ]
    for text in samples:
        for limit in (5, 10, 20, 50):
            parts = split_text_for_chunks(text, limit, 3)
            check("".join(parts) == text,
                  f"limit={limit} 且长度 {len(text)} 时不丢内容（{len(parts)} 段）")


def test_engine_chunking(tmp: Path) -> None:
    print("引擎分段与拼接")
    (tmp / "models").mkdir(parents=True, exist_ok=True)
    (tmp / "models" / "MyVoice.pth").write_bytes(b"fake")
    config = AppConfig.load(make_config(tmp, max_chars=20))
    engine = TTSEngine(config, AudioStorage(config.output_dir, expire_minutes=10))

    short = "你好呀。"
    check(engine.chunk_text(short) == [short], "短文本不触发分段")
    check(engine.chunk_plan(short) == 1, "短文本预估 1 段")

    long_text = "这是一句测试文本。" * 10          # 90 字
    chunks = engine.chunk_text(long_text)
    check(len(chunks) > 1, f"长文本被分为 {len(chunks)} 段")
    check(all(len(c) <= 20 for c in chunks), "每段不超过 chunk.max_chars")
    check("".join(chunks) == long_text, "分段后拼回去与原文一致")
    check(engine.chunk_plan(long_text) >= len(chunks), "预估段数不小于实际段数（用于超时估算）")

    # 分段关闭时应当完全不切
    engine.config.chunk["enabled"] = False
    check(engine.chunk_text(long_text) == [long_text], "chunk.enabled=false 时不分段")
    engine.config.chunk["enabled"] = True

    # 多段音频拼接
    if shutil.which("ffmpeg") is None:
        print("  [SKIP] 本机没有 ffmpeg，跳过音频拼接验证")
        return
    engine.storage.ensure_dirs()
    parts = []
    for index, seconds in enumerate((0.5, 0.3, 0.7)):
        part = engine.storage.work_dir / f"unit-{index}.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={seconds}",
             "-ar", "40000", "-ac", "1", str(part)],
            check=True, capture_output=True,
        )
        parts.append(part)
    merged = engine._concat_audio(parts)
    with wave.open(str(merged)) as handle:
        duration = handle.getnframes() / handle.getframerate()
    check(abs(duration - 1.5) < 0.15, f"拼接后时长 ≈ 1.5s（实际 {duration:.2f}s）")
    check(merged.stat().st_size > 512, "拼接结果不是空文件")


def test_backend_order(tmp: Path) -> None:
    print("语音源回退顺序")
    work = tmp / "auto"
    (work / "models").mkdir(parents=True, exist_ok=True)
    (work / "models" / "MyVoice.pth").write_bytes(b"fake")
    config = AppConfig.load(make_config(work, source="auto"))
    engine = TTSEngine(config, AudioStorage(config.output_dir, expire_minutes=10))
    engine._sapi_supported = lambda: False
    engine._find_espeak = lambda: "/usr/bin/espeak-ng"
    backends = engine._tts_backends()
    check(backends[0] == "edgetts", "auto 优先用在线语音")
    check(backends[-1] == "espeak", "auto 的最后一档是本地 espeak-ng")

    engine._find_espeak = lambda: None
    check(engine._tts_backends() == ["edgetts"], "没有任何本地语音时只留在在线语音")

    engine._sapi_supported = lambda: True
    check(engine._tts_backends() == ["edgetts", "sapi"], "有 SAPI 时优先用 SAPI 兜底")

    # 别名归一化
    for alias, expected in (("local", "sapi"), ("offline", "sapi"), ("espeak-ng", "espeak")):
        cfg = AppConfig.load(make_config(tmp / "auto", source=alias))
        eng = TTSEngine(cfg, AudioStorage(cfg.output_dir, expire_minutes=10))
        # Windows 上 local/offline 归一化成 sapi，Linux 上是 espeak；这里只校验不会崩
        check(eng.tts_source() in ("sapi", "espeak"), f"别名 {alias} 归一化正常（→{eng.tts_source()}）")


def main() -> int:
    print("=== 长文本分段与语音源回退自测 ===")
    test_split_rules()
    test_no_content_loss()
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        (tmp / "engine").mkdir()
        test_engine_chunking(tmp / "engine")
        test_backend_order(tmp)
    print()
    if FAILURES:
        print(f"❌ 失败 {len(FAILURES)} 项:")
        for item in FAILURES:
            print(f"   - {item}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
