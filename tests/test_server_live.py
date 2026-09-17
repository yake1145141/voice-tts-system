"""启动真实的 uvicorn 服务进程做端到端验证（可选，需要联网使用 Edge TTS）。

    python tests/test_server_live.py

流程：写临时配置（rvc.enabled=false）→ 启动 python main.py → 轮询 /api/health
      → 鉴权校验 → 生成音频 → 下载音频 → 关闭进程。

如果端口被占用或环境不支持（例如无法联网），脚本会打印原因并以 0 退出。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "tts-server"
PORT = int(os.environ.get("LIVE_TEST_PORT", "18099"))
API_KEY = "live-test-key"

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  [PASS] {message}")
    else:
        print(f"  [FAIL] {message}")
        FAILURES.append(message)


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def request(method: str, url: str, payload: dict | None = None, key: str | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def main() -> int:
    print("=== 语音处理端 真实进程端到端测试 ===")
    if not port_free(PORT):
        print(f"跳过：端口 {PORT} 已被占用")
        return 0

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        config_path = tmp / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "server:",
                    '  host: "127.0.0.1"',
                    f"  port: {PORT}",
                    '  log_level: "INFO"',
                    "security:",
                    f'  api_key: "{API_KEY}"',
                    "tts:",
                    '  source: "edgetts"',
                    '  speaker: "zh-CN-YunxiNeural"',
                    "  pitch: 5",
                    "rvc:",
                    "  enabled: false",  # 无 GPU/模型也能验证完整 HTTP 链路
                    '  model: ""',
                    "  preload: false",
                    "storage:",
                    f"  output_dir: '{(tmp / 'output').as_posix()}'",
                    "  expire_minutes: 10",
                    "  cleanup_interval_minutes: 1",
                    "queue:",
                    "  max_concurrent: 2",
                    "  max_queue_size: 4",
                    "  timeout: 120",
                ],
            ),
            encoding="utf-8",
        )

        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        log_path = tmp / "server.log"
        with log_path.open("wb") as log_file:
            process = subprocess.Popen(
                [sys.executable, "main.py", "--config", str(config_path)],
                cwd=str(SERVER_DIR),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
            )
        try:
            base = f"http://127.0.0.1:{PORT}"
            ready = False
            deadline = time.time() + 90
            while time.time() < deadline:
                if process.poll() is not None:
                    break
                try:
                    status, body, _ = request("GET", f"{base}/api/health")
                    if status == 200 and json.loads(body).get("success"):
                        ready = True
                        break
                except Exception:
                    time.sleep(1)
            check(ready, "服务启动并通过 /api/health 健康检查")
            if not ready:
                print("----- 服务日志 -----")
                print(log_path.read_text(encoding="utf-8", errors="ignore")[-3000:])
                return 1

            body = json.loads(request("GET", f"{base}/api/health")[1])
            check("engine" in body, "健康检查返回引擎信息")
            check(body["auth_enabled"] is True, "服务端鉴权已开启")

            status, _, _ = request("POST", f"{base}/api/tts", {"text": "你好"})
            check(status == 401, "无 API Key 被拒绝（401）")

            status, resp_body, _ = request(
                "POST",
                f"{base}/api/tts",
                {"text": "你好，这是语音测试。"},
                key=API_KEY,
            )
            check(status == 200, "带 API Key 的 JSON 请求返回 200")
            payload = json.loads(resp_body) if status == 200 else {}
            check(bool(payload.get("audio_url")), f"返回 audio_url: {payload.get('audio_url')}")

            status, audio, headers = request(
                "POST",
                f"{base}/api/tts/file",
                {"text": "直接返回音频文件的测试。"},
                key=API_KEY,
            )
            check(status == 200, "文件接口返回 200")
            check(headers.get("content-type", "").startswith("audio/"), "Content-Type 为音频")
            check(
                headers.get("x-audio-expire-minutes") == "10"
                or headers.get("X-Audio-Expire-Minutes") == "10",
                f"响应头声明保留 10 分钟（实际 {headers.get('x-audio-expire-minutes')}）",
            )
            check(len(audio) > 4000, f"音频数据大小 {len(audio)} 字节")

            status, resp_body, _ = request(
                "POST",
                f"{base}/api/tts",
                {"text": "验证保留时长字段。"},
                key=API_KEY,
            )
            payload2 = json.loads(resp_body)
            check(payload2.get("expire_minutes") == 10, "JSON 响应包含 10 分钟保留时长")
            health = json.loads(request("GET", f"{base}/api/health")[1])
            check(health["storage"]["expire_minutes"] == 10, "健康检查报告 10 分钟保留")

            path = str(payload.get("audio_path") or "")
            if path:
                status, downloaded, _ = request("GET", f"{base}{path}")
                check(status == 200 and len(downloaded) > 4000, "audio_url 可以正常下载")

            status, _, _ = request("POST", f"{base}/api/tts", {"text": ""}, key=API_KEY)
            check(status == 400, "空文本返回 400")

            # ---- 本地测试客户端（tools/tts_client.py）端到端 ----
            print("测试客户端 CLI")
            client_script = str(ROOT / "tools" / "tts_client.py")
            common = [sys.executable, client_script, "--url", base, "--api-key", API_KEY]

            proc = subprocess.run(
                [*common, "health"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=120,
            )
            check(proc.returncode == 0, f"health 子命令退出码 0（实际 {proc.returncode}）")
            check("引擎就绪: True" in proc.stdout, "health 输出引擎就绪状态")
            check("音频保留" in proc.stdout, "health 输出音频保留时长")

            out_dir = tmp / "client_out"
            proc = subprocess.run(
                [*common, "say", "你好，这是测试客户端合成的声音。", "--output-dir", str(out_dir)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=300,
            )
            check(proc.returncode == 0, f"say 子命令退出码 0（实际 {proc.returncode}）")
            check("保存文件" in proc.stdout, "say 保存了音频文件")
            files = list(out_dir.glob("*.wav")) if out_dir.exists() else []
            check(bool(files) and files[0].stat().st_size > 4000, "say 生成的音频文件有效")
            check("实时倍率" in proc.stdout, "say 输出实时倍率")

            proc = subprocess.run(
                [*common, "say", "URL 模式测试。", "--mode", "url", "--output-dir", str(out_dir)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=300,
            )
            check(proc.returncode == 0 and "audio_url" in proc.stdout, "say --mode url 返回并下载 audio_url")

            text_file = tmp / "texts.txt"
            text_file.write_text(
                "# 注释行会被忽略\n第一句话。\n\n第二句话（带括号动作）。\n第三句话。\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [*common, "batch", str(text_file), "-c", "2", "--output-dir", str(out_dir)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=600,
            )
            check(proc.returncode == 0, f"batch 子命令退出码 0（实际 {proc.returncode}）")
            check("成功 3/3" in proc.stdout, "batch 处理了 3 条文本")

            proc = subprocess.run(
                [*common, "bench", "--count", "4", "--concurrency", "2", "--warmup", "0"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=600,
            )
            check(proc.returncode == 0, f"bench 子命令退出码 0（实际 {proc.returncode}）")
            check("吞吐" in proc.stdout and "延迟(秒)" in proc.stdout, "bench 输出吞吐与延迟统计")

            proc = subprocess.run(
                [*common, "filter", "你好呀！（开心地笑）[挥手]"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=60,
            )
            check(proc.returncode == 0 and "你好呀！" in proc.stdout, "filter 子命令预览过滤结果")

            # 用错误的 Key 请求需要鉴权的接口：客户端应给出明确的 unauthorized 提示（退出码 1）
            # （/api/health 本身是开放的，便于做监控探活）
            wrong_env = {**os.environ, "TTS_SERVER_API_KEY": "definitely-wrong-key"}
            proc = subprocess.run(
                [sys.executable, client_script, "--url", base, "say", "鉴权测试"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=120,
                env=wrong_env,
            )
            check(
                proc.returncode == 1 and "unauthorized" in proc.stdout + proc.stderr,
                f"客户端在 Key 错误时给出明确错误（exit={proc.returncode}）",
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()

    print()
    if FAILURES:
        print(f"❌ 失败 {len(FAILURES)} 项:")
        for item in FAILURES:
            print(f"   - {item}")
        return 1
    print("✅ 端到端测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
