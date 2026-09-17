"""显存不足时的行为测试（用桩，不需要真的 1GB 显卡）。

覆盖：
  1. rvc.device=auto 且显存低于 rvc.min_vram_mb → 自动选择 CPU（并给出日志）
  2. 运行中抛 CUDA OOM → 自动降级到 CPU、重建实例、重试一次成功
  3. rvc.allow_cpu_fallback=false 时不做降级（把错误如实上报）
  4. rvc.device=cuda:0 显式指定时不会被自动改成 CPU（用户说了算）

运行：python tests/test_low_vram.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tts-server"))

from config import AppConfig  # noqa: E402
from engine import TTSEngine, TTSError  # noqa: E402
from storage import AudioStorage  # noqa: E402

FAILURES: list[str] = []

FAKE_WAV = (
    b"RIFF" + b"\x00" * 3000
)


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  [PASS] {message}")
    else:
        print(f"  [FAIL] {message}")
        FAILURES.append(message)


def make_config(tmp: Path, **rvc_overrides) -> AppConfig:
    (tmp / "models").mkdir(parents=True, exist_ok=True)
    (tmp / "models" / "MyVoice.pth").write_bytes(b"fake")
    rvc_lines = [
        "  enabled: true",
        '  model: "MyVoice.pth"',
        '  model_dir: "./models"',
        "  preload: false",
    ]
    for key, value in rvc_overrides.items():
        if isinstance(value, bool):
            rvc_lines.append(f"  {key}: {'true' if value else 'false'}")
        else:
            rvc_lines.append(f"  {key}: {value}")
    config_path = tmp / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "server:",
                '  host: "127.0.0.1"',
                "security:",
                '  api_key: ""',
                "tts:",
                '  source: "edgetts"',
                '  speaker: "zh-CN-YunxiNeural"',
                "rvc:",
                *rvc_lines,
                "storage:",
                f"  output_dir: '{(tmp / 'output').as_posix()}'",
                "queue:",
                "  max_concurrent: 1",
            ],
        ),
        encoding="utf-8",
    )
    return AppConfig.load(config_path)


class FakeTTS:
    """第一次调用抛 CUDA OOM，之后正常产出 wav。"""

    def __init__(self, fail_times: int = 1, error: str = "CUDA out of memory. Tried to allocate 512.00 MiB"):
        self.fail_times = fail_times
        self.error = error
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(self.error)
        target = Path(kwargs["output_filename"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(FAKE_WAV * 4)
        return str(target)


def test_low_vram_chooses_cpu() -> None:
    print("显存太小（auto）→ 自动用 CPU")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        # min_vram_mb 设得很大，模拟"这张卡显存不够"
        config = make_config(tmp, device="auto", min_vram_mb=999999)
        engine = TTSEngine(config, AudioStorage(config.output_dir, expire_minutes=10))
        device = engine._resolve_device()
        check(device == "cpu", f"auto 模式在低显存下选择 CPU（实际 {device}）")
        check(engine._force_cpu is True, "已记住强制 CPU，避免下次又去试 GPU")


def test_explicit_cuda_is_respected() -> None:
    print("显式 device=cuda:0 → 不会自动改成 CPU")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        config = make_config(tmp, device="cuda:0", min_vram_mb=999999)
        engine = TTSEngine(config, AudioStorage(config.output_dir, expire_minutes=10))
        device = engine._resolve_device()
        check(device == "cuda:0", f"显式指定 cuda:0 被尊重（实际 {device}）")


def test_oom_fallback_and_retry() -> None:
    print("运行中 OOM → 降级 CPU 并重试成功")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        config = make_config(tmp, device="cuda:0", allow_cpu_fallback=True)
        storage = AudioStorage(config.output_dir, expire_minutes=10)
        engine = TTSEngine(config, storage)
        engine._device = "cuda:0"
        engine.ready = True

        fake = FakeTTS(fail_times=1)
        engine._tts = fake

        # 降级后会调用 _initialize_sync 重建实例；测试里用同一个假实现替代
        def fake_init() -> None:
            engine._tts = fake

        engine._initialize_sync = fake_init  # type: ignore[method-assign]

        path = engine._synthesize_sync("测试文本", "a" * 32 + ".wav")
        check(path.exists(), "降级后重试成功并生成了文件")
        check(fake.calls == 2, f"总共调用了 2 次（1 次 OOM + 1 次成功），实际 {fake.calls}")
        check(engine.status()["device"] == "cpu", "状态里设备已变成 cpu")
        check(engine.status()["device_fallback"] is True, "状态里标记了 device_fallback")
        check(engine.status()["is_half"] is False, "CPU 上半精度已关闭")


def test_oom_without_fallback() -> None:
    print("allow_cpu_fallback=false → 如实上报错误")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        config = make_config(tmp, device="cuda:0", allow_cpu_fallback=False)
        storage = AudioStorage(config.output_dir, expire_minutes=10)
        engine = TTSEngine(config, storage)
        engine._device = "cuda:0"
        engine.ready = True
        fake = FakeTTS(fail_times=99)
        engine._tts = fake
        try:
            engine._synthesize_sync("测试", "b" * 32 + ".wav")
            check(False, "应该抛出 TTSError")
        except TTSError as exc:
            check("out of memory" in exc.message.lower(), f"错误信息保留 OOM 细节（{exc.message[:60]}）")
        check(engine._device == "cuda:0", "未降级，设备仍是 cuda:0")


def test_gpu_unusable_markers() -> None:
    """老显卡架构不被支持时抛出的异常，也要能识别并降级。"""
    print("识别「这张卡用不了」的多种报错")
    cases = [
        "CUDA out of memory. Tried to allocate 512.00 MiB",
        "CUDA error: operation not supported",
        "CUDA error: no kernel image is available for execution on the device",
        "GRID P40-1Q with CUDA capability sm_61 is not compatible with the current PyTorch installation",
        "CUDA error: invalid device function",
        "RuntimeError: CUDA driver version is insufficient for CUDA runtime version",
    ]
    for message in cases:
        check(TTSEngine._is_gpu_unusable(RuntimeError(message)), f"能识别：{message[:48]}…")
    check(
        not TTSEngine._is_gpu_unusable(RuntimeError("text too long")),
        "普通错误不会被误判为显卡问题",
    )


def test_old_arch_fallback_flow() -> None:
    """模拟老卡：运行时报 operation not supported → 降级 CPU 并重试。"""
    print("老显卡（sm_61 内核不支持）→ 降级 CPU 重试")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        config = make_config(tmp, device="cuda:0", allow_cpu_fallback=True)
        storage = AudioStorage(config.output_dir, expire_minutes=10)
        engine = TTSEngine(config, storage)
        engine._device = "cuda:0"
        engine.ready = True

        fake = FakeTTS(fail_times=1, error="CUDA error: operation not supported")
        engine._tts = fake
        engine._initialize_sync = lambda: setattr(engine, "_tts", fake)  # type: ignore[method-assign]

        path = engine._synthesize_sync("测试", "c" * 32 + ".wav")
        check(path.exists(), "老卡报错后重试成功")
        check(engine.status()["device"] == "cpu", "已切换到 CPU")
        check(engine.status()["device_fallback"] is True, "device_fallback 已标记")


def test_arch_check_logic() -> None:
    """检测"显卡架构是否被当前 PyTorch 支持"的逻辑。"""
    print("架构支持性检测")
    try:
        import torch

        if not torch.cuda.is_available():
            print("  [SKIP] 本机没有可用 CUDA，跳过")
            return
    except Exception:
        print("  [SKIP] 未安装 torch，跳过")
        return

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        config = make_config(tmp, device="auto")
        engine = TTSEngine(config, AudioStorage(config.output_dir, expire_minutes=10))

        original_arch = torch.cuda.get_arch_list
        original_cap = torch.cuda.get_device_capability
        try:
            # 假装当前 torch 只编译了 sm_75+，而显卡是 Pascal sm_61
            torch.cuda.get_arch_list = lambda: ["sm_75", "sm_80", "sm_90"]  # type: ignore[assignment]
            torch.cuda.get_device_capability = lambda dev=0: (6, 1)  # type: ignore[assignment]
            usable, detail = engine._check_cuda_supported()
            check(usable is False, f"sm_61 老卡被判定为不支持（{detail[:60]}）")

            torch.cuda.get_device_capability = lambda dev=0: (7, 5)  # type: ignore[assignment]
            usable2, _ = engine._check_cuda_supported()
            check(usable2 is True, "sm_75 被判定为支持")

            # cu121 的 torch 编译了 sm_50/sm_60（没有 sm_61），
            # 按 CUDA 二进制兼容性规则，sm_61 的 P106/P4 可以直接复用 sm_60 内核。
            torch.cuda.get_arch_list = lambda: ["sm_50", "sm_60", "sm_70", "sm_75", "sm_80", "sm_86", "sm_90"]  # type: ignore[assignment]
            torch.cuda.get_device_capability = lambda dev=0: (6, 1)  # type: ignore[assignment]
            usable3, detail3 = engine._check_cuda_supported()
            check(usable3 is True, f"sm_61 复用 sm_60 内核被判定为可用（{detail3[:60]}）")
        finally:
            torch.cuda.get_arch_list = original_arch  # type: ignore[assignment]
            torch.cuda.get_device_capability = original_cap  # type: ignore[assignment]


def main() -> int:
    print("=== 显存不足行为测试 ===")
    test_low_vram_chooses_cpu()
    test_explicit_cuda_is_respected()
    test_oom_fallback_and_retry()
    test_oom_without_fallback()
    test_gpu_unusable_markers()
    test_old_arch_fallback_flow()
    test_arch_check_logic()
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
