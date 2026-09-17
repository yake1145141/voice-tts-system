"""语音处理端自测脚本（不依赖 GPU / RVC 模型 / 网络）。

运行：
    python tests/test_server_api.py            # 使用桩函数，秒级完成
    python tests/test_server_api.py --real     # 额外做一次真实的 Edge TTS 合成（需要联网）

覆盖：鉴权、JSON/文件两种返回、缓存复用、参数校验、错误码、音频下载、
      路径穿越防护、过期文件清理。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "tts-server"
sys.path.insert(0, str(SERVER_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from config import AppConfig  # noqa: E402
from engine import TTSEngine  # noqa: E402
from main import build_app  # noqa: E402
from storage import AudioStorage  # noqa: E402

API_KEY = "test-secret-key"

FAKE_WAV = (
    b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    + b"data" + struct.pack("<I", 0)
)


def write_fake_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 写入 > 512 字节，模拟真实音频，通过引擎的最小体积校验
    path.write_bytes(FAKE_WAV + b"\x00" * 2048)


def make_config(tmp: Path, **overrides) -> Path:
    config_path = tmp / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "server:",
                '  host: "127.0.0.1"',
                "  port: 18080",
                "  log_level: \"WARNING\"",
                "security:",
                f'  api_key: "{API_KEY}"',
                "tts:",
                '  source: "edgetts"',
                '  speaker: "zh-CN-YunxiNeural"',
                "  pitch: 5",
                "rvc:",
                "  enabled: false",
                '  model: ""',
                "  preload: false",
                "storage:",
                f"  output_dir: '{(tmp / 'output').as_posix()}'",
                "  expire_minutes: 10",
                "  max_text_length: 50",
                "queue:",
                "  max_concurrent: 2",
                "  max_queue_size: 4",
                "  timeout: 30",
            ],
        ),
        encoding="utf-8",
    )
    return config_path


def check(condition: bool, message: str, failures: list[str]) -> None:
    if condition:
        print(f"  [PASS] {message}")
    else:
        print(f"  [FAIL] {message}")
        failures.append(message)


def run_stub_tests(failures: list[str]) -> None:
    calls = {"count": 0}
    original = TTSEngine._synthesize_sync

    def fake_synthesize(self: TTSEngine, text: str, filename: str) -> Path:
        calls["count"] += 1
        target = self.storage.work_dir / f"fake-{calls['count']}.wav"
        write_fake_wav(target)
        return self.storage.finalize(target, filename)

    TTSEngine._synthesize_sync = fake_synthesize  # type: ignore[method-assign]
    try:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = AppConfig.load(make_config(tmp))
            app = build_app(config)
            with TestClient(app, raise_server_exceptions=False) as client:
                print("鉴权")
                resp = client.post("/api/tts", json={"text": "你好"})
                check(resp.status_code == 401, "缺少 API Key 返回 401", failures)
                check(
                    resp.json()["error"]["code"] == "unauthorized",
                    "401 响应包含错误码",
                    failures,
                )
                resp = client.post(
                    "/api/tts",
                    json={"text": "你好"},
                    headers={"X-API-Key": "wrong"},
                )
                check(resp.status_code == 401, "错误 API Key 返回 401", failures)
                resp = client.get(
                    "/api/tts",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
                check(resp.status_code == 405, "鉴权接口不接受 GET", failures)

                print("健康检查")
                resp = client.get("/api/health")
                check(resp.status_code == 200, "/api/health 可用", failures)
                check(resp.json()["engine"]["ready"] is True, "引擎状态 ready", failures)

                print("JSON 模式")
                headers = {"Authorization": f"Bearer {API_KEY}"}
                resp = client.post("/api/tts", json={"text": "你好，这是语音测试。"}, headers=headers)
                check(resp.status_code == 200, "POST /api/tts 返回 200", failures)
                body = resp.json()
                check(body["success"] is True, "success = true", failures)
                check(
                    body["audio_path"].startswith("/audio/") and body["audio_url"].endswith(body["audio_path"]),
                    f"audio_url / audio_path 可用: {body['audio_url']}",
                    failures,
                )
                check(body["filename"].endswith(".wav"), "返回 wav 文件名", failures)
                check(calls["count"] == 1, "生成了一次音频", failures)

                print("缓存复用")
                resp = client.post("/api/tts", json={"text": "你好，这是语音测试。"}, headers=headers)
                check(resp.json()["cached"] is True, "重复文本命中缓存", failures)
                check(calls["count"] == 1, "缓存命中时不再推理", failures)

                print("音频下载")
                resp = client.get(body["audio_url"])
                check(resp.status_code == 200, "GET audio_url 返回 200", failures)
                check(resp.headers["content-type"].startswith("audio/"), "音频 Content-Type 正确", failures)
                check(len(resp.content) > 512, "音频内容非空", failures)
                resp = client.get("/audio/deadbeef.wav")
                check(resp.status_code == 404, "不存在的音频返回 404", failures)
                resp = client.get("/audio/..%2Fconfig.yaml")
                check(resp.status_code == 404, "路径穿越被拒绝", failures)

                print("文件模式")
                resp = client.post(
                    "/api/tts/file",
                    json={"text": "直接返回音频文件"},
                    headers=headers,
                )
                check(resp.status_code == 200, "POST /api/tts/file 返回 200", failures)
                check(resp.headers["content-type"].startswith("audio/"), "返回音频流", failures)
                check("X-Audio-Filename" in resp.headers, "返回 X-Audio-Filename 头", failures)
                resp = client.post(
                    "/api/tts?format=file",
                    json={"text": "带查询参数的文件模式"},
                    headers=headers,
                )
                check(resp.headers["content-type"].startswith("audio/"), "?format=file 也返回音频", failures)

                print("参数校验")
                resp = client.post("/api/tts", json={"text": "   "}, headers=headers)
                check(resp.status_code == 400, "空文本返回 400", failures)
                check(resp.json()["error"]["code"] == "empty_text", "空文本错误码正确", failures)
                resp = client.post("/api/tts", json={"text": "长" * 80}, headers=headers)
                check(resp.status_code == 413, "超长文本返回 413", failures)
                resp = client.post("/api/tts", json={}, headers=headers)
                check(resp.status_code == 422, "缺少 text 字段返回 422", failures)
                check(resp.json()["error"]["code"] == "invalid_request", "422 错误码统一", failures)

                print("失败降级")

                def boom(self: TTSEngine, text: str, filename: str) -> Path:
                    raise RuntimeError("模拟 RVC 崩溃")

                TTSEngine._synthesize_sync = boom  # type: ignore[method-assign]
                resp = client.post("/api/tts", json={"text": "会失败的文本"}, headers=headers)
                check(resp.status_code == 500, "生成失败返回 500", failures)
                check(
                    resp.json()["error"]["code"] == "rvc_failed",
                    "失败响应包含明确错误码",
                    failures,
                )
                TTSEngine._synthesize_sync = fake_synthesize  # type: ignore[method-assign]

            print("过期清理（默认 10 分钟）")
            check(
                abs(config.expire_minutes - 10) < 0.001,
                f"配置解析出 10 分钟保留时长（实际 {config.expire_minutes}）",
                failures,
            )
            storage = AudioStorage(config.output_dir, expire_minutes=10)
            check(
                abs(storage.expire_seconds - 600) < 0.001,
                f"过期秒数为 600（实际 {storage.expire_seconds}）",
                failures,
            )
            storage.ensure_dirs()
            # 11 分钟前的文件应被删除，5 分钟前的应保留
            old = storage.save_bytes("a" * 32 + ".wav", b"x" * 32)
            os.utime(old, (time.time() - 11 * 60, time.time() - 11 * 60))
            fresh = storage.save_bytes("b" * 32 + ".wav", b"x" * 32)
            os.utime(fresh, (time.time() - 5 * 60, time.time() - 5 * 60))
            result = storage.cleanup()
            check(result["removed_audio"] == 1, "11 分钟前的音频被清理", failures)
            check(not old.exists(), "超过 10 分钟的文件已删除", failures)
            check(fresh.exists(), "5 分钟内的文件保留", failures)
            check(storage.is_fresh(fresh), "未过期文件判定为 fresh", failures)
            check(storage.stats()["expire_minutes"] == 10, "统计信息报告 10 分钟", failures)
            check(not storage.resolve("../../etc/passwd"), "非法文件名被拒绝", failures)
    finally:
        TTSEngine._synthesize_sync = original  # type: ignore[method-assign]


def run_real_test(failures: list[str]) -> None:
    """真实调用 Edge TTS，验证 zh-CN-YunxiNeural 配置是否可用（不经过 RVC）。"""
    print("真实 Edge TTS 合成（rvc.enabled=false 调试模式）")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        config = AppConfig.load(make_config(tmp))
        config.data["rvc"]["enabled"] = False
        config.rvc["enabled"] = False
        app = build_app(config)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/tts",
                json={"text": "你好，这是语音测试。"},
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            check(resp.status_code == 200, f"真实合成返回 200（实际 {resp.status_code}）", failures)
            if resp.status_code == 200:
                body = resp.json()
                audio = client.get(body["audio_url"])
                check(len(audio.content) > 2000, "生成了真实音频数据", failures)


def run_rvc_mapping_test(failures: list[str]) -> None:
    """校验配置到 tts-with-rvc 参数的映射（用假 TTS 对象，不需要 GPU/模型推理）。"""
    print("tts-with-rvc 参数映射")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        (tmp / "models").mkdir(parents=True, exist_ok=True)
        (tmp / "models" / "MyVoice.pth").write_bytes(b"fake")
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
                    "  pitch: 5",
                    "  rate: -5",
                    "rvc:",
                    "  enabled: true",
                    '  model: "MyVoice.pth"',
                    '  model_dir: "./models"',
                    "  index_rate: 0.9",
                    '  f0_method: "rmvpe"',
                    '  device: "cpu"',
                    "  preload: false",
                    "storage:",
                    f"  output_dir: '{(tmp / 'output').as_posix()}'",
                ],
            ),
            encoding="utf-8",
        )
        config = AppConfig.load(config_path)
        check(config.model_path is not None and config.model_path.exists(), "解析到 RVC 模型路径", failures)

        captured: dict = {}

        class FakeTTS:
            def set_voice(self, voice: str) -> None:  # pragma: no cover
                pass

            def __call__(self, **kwargs):
                captured.update(kwargs)
                target = Path(config.output_dir) / ".work" / "fake.wav"
                write_fake_wav(target)
                return str(target)

        storage = AudioStorage(config.output_dir, 24)
        engine = TTSEngine(config, storage)
        engine._tts = FakeTTS()
        engine._device = "cpu"
        engine._is_half = False
        engine.ready = True
        filename = f"{engine.cache_key('你好世界')}.wav"
        path = engine._synthesize_sync("你好世界", filename)

        check(path.exists(), "生成文件已落盘", failures)
        check(captured.get("text") == "你好世界", "text 透传正确", failures)
        check(captured.get("pitch") == 5, f"RVC 音调固定为 5（实际 {captured.get('pitch')}）", failures)
        check(captured.get("f0method") == "rmvpe", "f0_method 透传正确", failures)
        check(captured.get("index_rate") == 0.9, "index_rate 透传正确", failures)
        check(captured.get("tts_rate") == -5, "语速透传正确", failures)
        check(captured.get("output_filename", "").endswith(filename), "输出文件名可控", failures)
        check(
            config.speaker == "zh-CN-YunxiNeural",
            "发音人固定为 zh-CN-YunxiNeural",
            failures,
        )


def run_library_signature_test(failures: list[str]) -> None:
    """校验 calls 的参数名与当前安装的 tts-with-rvc 版本一致（防止库升级后参数漂移）。"""
    print("tts-with-rvc 库接口对齐")
    try:
        import inspect

        from tts_with_rvc import TTS_RVC
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] 未安装 tts-with-rvc（{exc}）")
        return

    init_params = set(inspect.signature(TTS_RVC.__init__).parameters)
    call_params = set(inspect.signature(TTS_RVC.__call__).parameters)

    for name in ("model_path", "voice", "index_path", "f0_method", "device", "output_directory"):
        check(name in init_params, f"TTS_RVC.__init__ 支持参数 {name}", failures)
    for name in (
        "text",
        "pitch",
        "tts_rate",
        "tts_volume",
        "tts_pitch",
        "output_filename",
        "index_rate",
        "is_half",
        "f0method",
        "filter_radius",
        "resample_sr",
        "rms_mix_rate",
        "protect",
    ):
        check(name in call_params, f"TTS_RVC.__call__ 支持参数 {name}", failures)
    check(hasattr(TTS_RVC, "set_voice"), "TTS_RVC 提供 set_voice()", failures)


def run_rvc_serialization_test(failures: list[str]) -> None:
    """验证 RVC 调用的串行化与失败自愈（tts-with-rvc 的全局状态不是线程安全的）。"""
    print("RVC 串行化与失败自愈")
    try:
        from tts_with_rvc import vc_infer
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] 未安装 tts-with-rvc（{exc}）")
        return

    import threading
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    from engine import TTSEngine, TTSError

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        (tmp / "models").mkdir(parents=True)
        (tmp / "models" / "MyVoice.pth").write_bytes(b"fake")
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
                    "  pitch: 5",
                    "rvc:",
                    "  enabled: true",
                    '  model: "MyVoice.pth"',
                    '  model_dir: "./models"',
                    '  device: "cpu"',
                    "  preload: false",
                    "storage:",
                    f"  output_dir: '{(tmp / 'output').as_posix()}'",
                    "queue:",
                    "  max_concurrent: 4",
                ],
            ),
            encoding="utf-8",
        )
        config = AppConfig.load(config_path)
        storage = AudioStorage(config.output_dir, expire_minutes=10)
        engine = TTSEngine(config, storage)

        state = {"now": 0, "max": 0, "calls": 0}
        guard = threading.Lock()
        fail_flag = {"fail": False}

        class FakeTTS:
            def __call__(self, **kwargs):
                with guard:
                    state["now"] += 1
                    state["max"] = max(state["max"], state["now"])
                    state["calls"] += 1
                try:
                    _time.sleep(0.15)
                    if fail_flag["fail"]:
                        raise RuntimeError("模拟库内部异常")
                    target = storage.work_dir / str(kwargs["output_filename"])
                    write_fake_wav(target)
                    return str(target)
                finally:
                    with guard:
                        state["now"] -= 1

        engine._tts = FakeTTS()
        engine._device = "cpu"
        engine.ready = True

        def call(index: int):
            return engine._synthesize_sync(f"并发测试{index}", f"{'a' * 31}{index}.wav")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(call, range(4)))

        check(state["calls"] == 4, f"4 个任务都执行了（实际 {state['calls']}）", failures)
        check(
            state["max"] == 1,
            f"同一时刻只有一个 RVC 调用（最大并发 {state['max']}，说明已串行化）",
            failures,
        )

        # 失败注入：库在异常路径不会复位 can_speak，我们的实现必须复位
        fail_flag["fail"] = True
        vc_infer.can_speak = False
        vc_infer.last_model_path = "some-old-model.pth"
        try:
            engine._synthesize_sync("失败测试", f"{'b' * 32}.wav")
            check(False, "异常被正确上报（未抛出 TTSError）", failures)
        except TTSError as exc:
            check(exc.code == "rvc_failed", f"失败被映射为 rvc_failed（实际 {exc.code}）", failures)

        check(
            vc_infer.can_speak is True,
            "失败后全局 can_speak 被复位（否则后续请求会永久卡死）",
            failures,
        )
        check(
            vc_infer.last_model_path == "",
            "失败后清空 last_model_path，下次调用会重新加载模型",
            failures,
        )
        check(engine._rvc_lock.locked() is False, "失败后锁已释放", failures)


def run_local_tts_source_tests(failures: list[str]) -> None:
    """语音源选择 + auto 自动兜底到本地 TTS（用假后端，不依赖 Windows SAPI / 网络）。"""
    print("语音源选择与本地 TTS 兜底")
    from engine import TTSEngine, TTSError

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        (tmp / "models").mkdir(parents=True, exist_ok=True)
        (tmp / "models" / "MyVoice.pth").write_bytes(b"fake")

        def build(source: str) -> tuple[TTSEngine, AudioStorage]:
            config_path = tmp / f"config-{source}.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "security:",
                        '  api_key: ""',
                        "tts:",
                        f'  source: "{source}"',
                        '  speaker: "zh-CN-YunxiNeural"',
                        "  pitch: 5",
                        "  retries: 0",
                        "rvc:",
                        "  enabled: true",
                        '  model: "MyVoice.pth"',
                        '  model_dir: "./models"',
                        '  device: "cpu"',
                        "  preload: false",
                        "storage:",
                        f"  output_dir: '{(tmp / ('out-' + source)).as_posix()}'",
                        "queue:",
                        "  max_concurrent: 1",
                        "  timeout: 30",
                    ],
                ),
                encoding="utf-8",
            )
            config = AppConfig.load(config_path)
            store = AudioStorage(config.output_dir, expire_minutes=10)
            return TTSEngine(config, store), store

        # 1) 配置别名归一化
        for alias, expected in [
            ("edgetts", "edgetts"),
            ("sapi", "sapi"),
            ("local", "sapi"),
            ("offline", "sapi"),
            ("auto", "auto"),
            ("fallback", "auto"),
        ]:
            engine, _ = build(alias)
            check(engine.tts_source() == expected, f"tts.source={alias} → {expected}", failures)

        # 2) 后端尝试顺序
        auto_engine, _ = build("auto")
        auto_engine._sapi_supported = lambda: True
        check(
            auto_engine._tts_backends() == ["edgetts", "sapi"],
            "auto 且本机有本地语音 → 先在线后本地",
            failures,
        )
        auto_engine._sapi_supported = lambda: False
        check(
            auto_engine._tts_backends() == ["edgetts"],
            "auto 且本机无本地语音（如 Linux）→ 只用在线",
            failures,
        )
        sapi_engine, _ = build("sapi")
        check(sapi_engine._tts_backends() == ["sapi"], "sapi → 只用本地语音", failures)

        # 3) auto 兜底：在线失败后自动改用本地，并且请求仍然成功
        engine, store = build("auto")
        store.ensure_dirs()
        engine._sapi_supported = lambda: True
        calls: list[str] = []

        def fake_run_backend(backend: str, text: str, filename: str) -> Path:
            calls.append(backend)
            if backend == "edgetts":
                raise TTSError(
                    "rvc_failed",
                    "tts-with-rvc 执行失败: No audio was received. Please verify that your parameters are correct.",
                    status_code=500,
                )
            raw = store.work_dir / f"sapi-{text}.wav"
            write_fake_wav(raw)
            return raw

        engine._run_backend = fake_run_backend  # type: ignore[method-assign]
        result = engine._synthesize_sync("兜底测试", f"{'c' * 32}.wav")
        check(calls == ["edgetts", "sapi"], f"在线失败后自动改用本地（实际 {calls}）", failures)
        check(result.exists() and result.stat().st_size > 512, "兜底后仍然产出了音频文件", failures)

        # 4) 显式只用在线时不应该偷偷兜底，失败就是失败
        edge_engine, edge_store = build("edgetts")
        edge_store.ensure_dirs()
        edge_calls: list[str] = []

        def edge_only(backend: str, text: str, filename: str) -> Path:
            edge_calls.append(backend)
            raise TTSError("rvc_failed", "tts-with-rvc 执行失败: No audio was received.", 500)

        edge_engine._run_backend = edge_only  # type: ignore[method-assign]
        try:
            edge_engine._synthesize_sync("只在线", f"{'d' * 32}.wav")
            check(False, "source=edgetts 时在线失败应直接报错", failures)
        except TTSError as exc:
            check(exc.code == "rvc_failed", f"source=edgetts 保留原错误码（实际 {exc.code}）", failures)
        check(edge_calls == ["edgetts"], "source=edgetts 不会尝试本地 TTS", failures)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="额外执行真实 Edge TTS 合成")
    args = parser.parse_args()

    failures: list[str] = []
    print("=== 语音处理端 API 自测 ===")
    run_stub_tests(failures)
    run_rvc_mapping_test(failures)
    run_library_signature_test(failures)
    run_rvc_serialization_test(failures)
    run_local_tts_source_tests(failures)
    if args.real:
        run_real_test(failures)

    print()
    if failures:
        print(f"❌ 失败 {len(failures)} 项:")
        for item in failures:
            print(f"   - {item}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
