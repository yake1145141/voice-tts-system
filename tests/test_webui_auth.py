"""网页控制台自测：状态接口、密码验证、会话 Cookie、登出。

    python tests/test_webui_auth.py

不依赖 GPU / RVC / 网络：rvc 关掉，用 FastAPI TestClient 直接打。
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "tts-server"
sys.path.insert(0, str(SERVER_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from config import AppConfig  # noqa: E402
from main import build_app  # noqa: E402

PASSWORD = "s3cret-pass"
USERNAME = "admin"

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  [PASS] {message}")
    else:
        print(f"  [FAIL] {message}")
        FAILURES.append(message)


def make_config(tmp: Path, *, password: str) -> Path:
    config_path = tmp / "config.yaml"
    lines = [
        "server:",
        '  host: "127.0.0.1"',
        "  port: 18081",
        '  log_level: "WARNING"',
        "security:",
        '  api_key: "api-key-123"',
        "tts:",
        '  source: "edgetts"',
        '  speaker: "zh-CN-YunxiNeural"',
        "rvc:",
        "  enabled: false",
        "  preload: false",
        "webui:",
        "  enabled: true",
        f'  username: "{USERNAME}"',
        f'  password: "{password}"',
        "  session_hours: 1",
        "storage:",
        f"  output_dir: '{(tmp / 'output').as_posix()}'",
        "  expire_minutes: 10",
        "queue:",
        "  max_concurrent: 1",
        "  timeout: 30",
    ]
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path


def run_no_password(tmp: Path) -> None:
    print("未设置密码：控制台直接可用（向后兼容）")
    config = AppConfig.load(make_config(tmp / "open", password=""))
    with TestClient(build_app(config)) as client:
        r = client.get("/", follow_redirects=False)
        check(r.status_code == 200 and "TTS 语音服务" in r.text, "GET / 直接返回控制台")
        check(client.get("/api/gpu").status_code == 200, "GET /api/gpu 无需登录")
        check(client.get("/api/stats").status_code == 200, "GET /api/stats 无需登录")
        check(client.get("/api/config", headers={"X-API-Key": "api-key-123"}).status_code == 200,
              "GET /api/config 用 API Key 可访问")


def run_with_password(tmp: Path) -> None:
    print("设置了密码：未登录一律挡在登录页")
    config = AppConfig.load(make_config(tmp / "locked", password=PASSWORD))
    with TestClient(build_app(config)) as client:
        r = client.get("/", follow_redirects=False)
        check(r.status_code == 302 and r.headers["location"] == "/login",
              f"未登录访问 / 被重定向到 /login（实际 {r.status_code} {r.headers.get('location')}）")

        r = client.get("/login")
        check(r.status_code == 200 and 'id="form"' in r.text, "GET /login 返回登录页")
        check("https://" not in r.text and "cdn" not in r.text.lower(), "登录页无外链")

        for path in ("/api/gpu", "/api/system", "/api/stats", "/api/config", "/api/cleanup"):
            method = client.post if path == "/api/cleanup" else client.get
            r = method(path)
            check(r.status_code == 401, f"未登录 {path} 返回 401（实际 {r.status_code}）")

        # 探活与语音接口不受网页密码影响
        check(client.get("/api/health").status_code == 200, "/api/health 保持开放（探活用）")
        r = client.post("/api/tts", json={"text": "你好"},
                        headers={"X-API-Key": "api-key-123"})
        check(r.status_code != 401, f"/api/tts 不受网页密码影响（实际 {r.status_code}）")
        check(client.post("/api/tts", json={"text": "你好"}).status_code == 401,
              "/api/tts 仍然要 API Key")

        # 错误密码
        r = client.post("/api/login", json={"username": USERNAME, "password": "wrong"})
        check(r.status_code == 401, f"密码错误返回 401（实际 {r.status_code}）")
        r = client.post("/api/login", json={"username": "root", "password": PASSWORD})
        check(r.status_code == 401, f"用户名错误返回 401（实际 {r.status_code}）")

        # 正确密码
        r = client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
        check(r.status_code == 200 and r.json().get("success") is True, "正确密码登录成功")
        cookie = r.cookies.get("tts_webui_session")
        check(bool(cookie), "登录后下发了会话 Cookie")

        r = client.get("/")
        check(r.status_code == 200 and "显卡状态" in r.text, "登录后能打开控制台")
        check(client.get("/api/gpu").status_code == 200, "登录后 /api/gpu 可用")
        check(client.get("/api/stats").json().get("webui_protected") is True,
              "/api/stats 标记 webui_protected=true")

        # 伪造 / 篡改 Cookie 必须被拒
        with TestClient(build_app(config)) as other:
            other.cookies.set("tts_webui_session", "ZmFrZQ.deadbeef")
            check(other.get("/", follow_redirects=False).status_code == 302,
                  "伪造的 Cookie 被拒绝")
            tampered = (cookie or "")[:-4] + "0000"
            other.cookies.set("tts_webui_session", tampered)
            check(other.get("/", follow_redirects=False).status_code == 302,
                  "被篡改签名的 Cookie 被拒绝")

        # 登出
        client.post("/api/logout")
        r = client.get("/", follow_redirects=False)
        check(r.status_code == 302, "登出后访问 / 又回到登录页")


def run_secret_persistence(tmp: Path) -> None:
    print("会话密钥持久化")
    config = AppConfig.load(make_config(tmp / "secret", password=PASSWORD))
    secret_file = config.path.parent / ".webui_secret"
    if secret_file.exists():
        secret_file.unlink()
    with TestClient(build_app(config)) as client:
        client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
        token_a = client.cookies.get("tts_webui_session")
    check(secret_file.exists(), ".webui_secret 已生成在 config.yaml 同目录")
    # 重新构建 app（模拟重启服务），旧 Cookie 依然有效
    config2 = AppConfig.load(config.path)
    with TestClient(build_app(config2)) as client:
        client.cookies.set("tts_webui_session", token_a)
        check(client.get("/", follow_redirects=False).status_code == 200,
              "重启服务后已登录的会话仍然有效")


def main() -> int:
    print("=== 网页控制台与密码验证自测 ===")
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        (tmp / "open").mkdir()
        (tmp / "locked").mkdir()
        (tmp / "secret").mkdir()
        run_no_password(tmp)
        run_with_password(tmp)
        run_secret_persistence(tmp)
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
