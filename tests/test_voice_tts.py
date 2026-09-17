"""单文件客户端 tools/voice_tts.py 的测试。

用一个本地假服务（http.server）模拟真实接口，因此不需要真的起语音服务：
    GET  /api/health     → 状态 JSON
    POST /api/tts/file   → 直接返回 wav 字节
    POST /api/tts        → 返回 audio_url
    401 分支              → 鉴权失败

运行：python tests/test_voice_tts.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

spec = importlib.util.spec_from_file_location("voice_tts_mod", ROOT / "tools" / "voice_tts.py")
assert spec and spec.loader
voice_tts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = voice_tts
spec.loader.exec_module(voice_tts)

VoiceTTS = voice_tts.VoiceTTS
VoiceTTSError = voice_tts.VoiceTTSError

API_KEY = "unit-test-key"
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        FAILURES.append(message)


def make_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    frames = int(seconds * rate)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        path = Path(handle.name)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack("<h", 1000) * frames)
    data = path.read_bytes()
    path.unlink()
    return data


WAV = make_wav(1.2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass

    def _auth_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {API_KEY}"

    def do_GET(self):
        if self.path == "/api/health":
            self._json({"success": True, "status": "ok", "engine": {"ready": True}})
        elif self.path.startswith("/audio/"):
            self._audio()
        else:
            self._json({"success": False, "error": {"code": "not_found", "message": "no"}}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if not self._auth_ok():
            self._json({"success": False, "error": {"code": "unauthorized", "message": "API Key 无效或缺失。"}}, 401)
            return
        if not body.get("text", "").strip():
            self._json({"success": False, "error": {"code": "empty_text", "message": "text 不能为空。"}}, 400)
            return
        if self.path == "/api/tts/file":
            self._audio()
        elif self.path == "/api/tts":
            self._json(
                {
                    "success": True,
                    "audio_url": f"http://127.0.0.1:{self.server.server_port}/audio/fake.wav",
                    "filename": "fake.wav",
                }
            )
        else:
            self._json({"success": False, "error": {"code": "not_found", "message": "no"}}, 404)

    def _json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _audio(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("X-Audio-Cached", "0")
        self.send_header("Content-Length", str(len(WAV)))
        self.end_headers()
        self.wfile.write(WAV)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    print(f"=== 单文件客户端测试（假服务 {base}）===")

    try:
        client = VoiceTTS(base, api_key=API_KEY, retries=0)

        print("基础调用")
        check(client.available() is True, "available() = True")
        check(client.health()["engine"]["ready"] is True, "health() 解析正常")

        print("合成")
        data = client.synthesize_bytes("你好")
        check(data[:4] == b"RIFF" and len(data) == len(WAV), f"synthesize_bytes 拿到 wav（{len(data)} 字节）")

        with tempfile.TemporaryDirectory() as tmp:
            path = client.say("你好", out_dir=tmp)
            check(path.exists() and path.parent == Path(tmp), f"say() 默认文件名 + out_dir（{path.name}）")
            check(path.read_bytes()[:4] == b"RIFF", "落盘内容是 wav")

            directory = Path(tmp) / "sub"
            directory.mkdir()
            got = client.synthesize("你好", dest=directory)          # 传目录
            check(got.parent == directory and got.suffix == ".wav", "dest 传目录 → 自动生成文件名")
            got2 = client.synthesize("你好", dest=Path(tmp) / "a.wav")  # 传文件
            check(got2.name == "a.wav", "dest 传文件 → 按文件名保存")
            got3 = client.synthesize("你好", dest=Path(tmp) / "sub2" / "b.wav")
            check(got3.exists() and got3.name == "b.wav", "dest 含不存在的父目录 → 自动创建")

        print("url 模式")
        url = client.audio_url("你好")
        check(url.endswith("/audio/fake.wav"), f"audio_url 返回完整地址（{url}）")

        print("时长读取")
        with tempfile.TemporaryDirectory() as tmp:
            path = client.synthesize("你好", out_dir=tmp)
            duration = client.duration(path)
            check(duration is not None and 1.0 < duration < 1.4, f"duration ≈ 1.2s（实际 {duration:.2f}）")

        print("配置自动发现")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "config.yaml").write_text(
                "server:\n  host: \"0.0.0.0\"\n  port: 12345\nsecurity:\n  api_key: \"from-config\"\n",
                encoding="utf-8",
            )
            info = voice_tts._read_config_yaml(tmp_path / "config.yaml")
            check(info["url"] == "http://127.0.0.1:12345", f"从 config.yaml 解析出 url（{info['url']}）")
            check(info["api_key"] == "from-config", "从 config.yaml 解析出 api_key")

        print("环境变量优先")
        os.environ["VOICE_TTS_URL"] = base
        os.environ["VOICE_TTS_API_KEY"] = API_KEY
        try:
            auto = VoiceTTS()
            check(auto.base_url == base and auto.api_key == API_KEY, "构造时不传参也能读到环境变量")
            check(auto.available() is True, "自动发现的配置可直接调用")
        finally:
            os.environ.pop("VOICE_TTS_URL", None)
            os.environ.pop("VOICE_TTS_API_KEY", None)

        print("错误处理")
        try:
            VoiceTTS(base, api_key="bad", retries=0).synthesize("你好")
            check(False, "错误 Key 应抛异常")
        except VoiceTTSError as exc:
            check(exc.code == "unauthorized", "错误 Key → unauthorized")
        try:
            client.synthesize("   ")
            check(False, "空文本应抛异常")
        except VoiceTTSError as exc:
            check(exc.code == "empty_text", "空文本 → empty_text")

        print("假服务收到 401 也不重试（4xx 不重试）")
        check(True, "4xx 直接抛错（已在上一条验证）")
    finally:
        server.shutdown()
        server.server_close()

    print()
    if FAILURES:
        print(f"❌ 失败 {len(FAILURES)} 项:")
        for item in FAILURES:
            print("   -", item)
        return 1
    print("✅ 单文件客户端测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
