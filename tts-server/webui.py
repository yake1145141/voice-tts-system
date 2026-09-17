"""网页管理面板：服务状态、显卡状态、在线试听、最近请求。

设计要点：
1. 页面本体是 static/index.html，零外链，断网也能打开（不依赖任何 CDN）。
2. 显卡状态走 nvidia-smi 查询（不额外引入 pynvml 依赖），带 2 秒缓存，
   避免面板高频刷新时反复 fork 子进程。
3. "/" 返回面板，"/api/" 仍返回原来的 JSON 索引，老调用方不受影响。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger("tts_server.webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "tts_webui_session"


# ---------------------------------------------------------------------------
# 网页控制台的密码验证
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class WebUIAuth:
    """控制台登录：用户名 + 密码换一个 HMAC 签名的会话 Cookie。

    * 不引入任何三方库（只用标准库 hmac/hashlib/base64）；
    * 密钥持久化在 config.yaml 同目录的 .webui_secret，重启服务不会把已登录的人踢掉；
    * ``webui.password`` 留空时整个机制自动关闭（浏览器直接打开控制台，和以前一样）。
    """

    def __init__(self, config: Any) -> None:
        section = getattr(config, "webui", None)
        enabled = section.bool("enabled", True) if section is not None else True
        self.username = (section.str("username", "admin").strip() or "admin") if section else "admin"
        self.password = section.str("password", "").strip() if section else ""
        hours = section.float("session_hours", 12) if section else 12.0
        self.session_seconds = max(60, int(hours * 3600))
        self.enabled = bool(enabled and self.password)
        self.secret = self._load_secret(config) if self.enabled else b""
        if self.enabled:
            logger.info(
                "网页控制台已启用密码验证（用户名 %s，会话 %d 小时）",
                self.username,
                self.session_seconds // 3600,
            )

    @staticmethod
    def _load_secret(config: Any) -> bytes:
        try:
            base = Path(getattr(config, "path", Path("config.yaml"))).parent
            path = base / ".webui_secret"
            if path.exists():
                data = path.read_bytes().strip()
                if len(data) >= 32:
                    return data
            data = secrets.token_bytes(32)
            path.write_bytes(data)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return data
        except OSError:
            logger.warning(
                "无法写入 .webui_secret，会话密钥只存在于内存中（重启服务后需要重新登录）"
            )
            return secrets.token_bytes(32)

    def check_credentials(self, username: str, password: str) -> bool:
        if not self.enabled:
            return True
        return hmac.compare_digest(username.strip(), self.username) and hmac.compare_digest(
            password, self.password
        )

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        payload = f"{self.username}:{int(time.time()) + self.session_seconds}"
        token = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{token}.{self._sign(payload)}"

    def verify(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token or "." not in token:
            return False
        encoded, _, signature = token.rpartition(".")
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        except Exception:
            return False
        if not hmac.compare_digest(signature, self._sign(payload)):
            return False
        username, _, expires = payload.rpartition(":")
        if username != self.username:
            return False
        try:
            return int(expires) > int(time.time())
        except ValueError:
            return False

    @staticmethod
    def needs_auth(path: str) -> bool:
        """哪些路径需要登录。"""
        if path in ("/", "/index.html"):
            return True
        if path.startswith("/docs") or path.startswith("/redoc") or path == "/openapi.json":
            return True
        if path in ("/api/login", "/api/logout", "/login"):
            return False
        # 语音接口由 API Key 保护（AstrBot 这类调用方没有浏览器 Cookie），不受网页密码影响
        if path.startswith("/api/tts"):
            return False
        # 探活接口保持开放，启动脚本 / 监控 / 负载均衡都要用
        if path == "/api/health":
            return False
        return path.startswith("/api/")

# ---------------------------------------------------------------------------
# GPU / 系统状态采集
# ---------------------------------------------------------------------------

_NVIDIA_SMI_QUERY = (
    "index,name,driver_version,utilization.gpu,utilization.memory,"
    "memory.used,memory.total,temperature.gpu,power.draw,power.limit,"
    "fan.speed,clocks.sm,clocks.mem"
)

_gpu_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_gpu_lock = threading.Lock()
_GPU_TTL = 2.0

_torch_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_TORCH_TTL = 30.0


def _run(cmd: list[str], timeout: float = 6.0) -> tuple[int, str]:
    """执行命令并返回 (exit_code, 合并后的输出)。任何异常都转成错误码，不抛出。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timeout"
    except Exception as exc:  # pragma: no cover - 极端环境
        return 1, f"{cmd[0]} error: {exc}"


def _num(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _torch_info() -> dict[str, Any]:
    now = time.time()
    cached = _torch_cache["data"]
    if cached is not None and now - _torch_cache["ts"] < _TORCH_TTL:
        return cached
    info: dict[str, Any] = {"installed": False}
    try:
        import torch

        info["installed"] = True
        info["version"] = torch.__version__
        info["cuda_available"] = bool(torch.cuda.is_available())
        info["cuda_version"] = getattr(torch.version, "cuda", None)
        try:
            info["arch_list"] = list(torch.cuda.get_arch_list())
        except Exception:
            info["arch_list"] = []
        if info["cuda_available"]:
            try:
                major, minor = torch.cuda.get_device_capability(0)
                info["capability"] = f"sm_{major}{minor}"
                info["device_count"] = torch.cuda.device_count()
            except Exception:
                pass
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    _torch_cache.update(ts=now, data=info)
    return info


def gpu_status(force: bool = False) -> dict[str, Any]:
    """返回显卡状态（默认 2 秒缓存）。没有 NVIDIA 卡时给出明确原因。"""
    now = time.time()
    with _gpu_lock:
        cached = _gpu_cache["data"]
        if not force and cached is not None and now - _gpu_cache["ts"] < _GPU_TTL:
            return cached

    result: dict[str, Any] = {"available": False, "gpus": [], "torch": _torch_info()}
    if shutil.which("nvidia-smi") is None:
        result["reason"] = "未找到 nvidia-smi（没有 NVIDIA 驱动，或容器里没挂 GPU）"
        with _gpu_lock:
            _gpu_cache.update(ts=now, data=result)
        return result

    code, out = _run(
        ["nvidia-smi", f"--query-gpu={_NVIDIA_SMI_QUERY}", "--format=csv,noheader,nounits"],
        timeout=6.0,
    )
    if code != 0:
        result["reason"] = f"nvidia-smi 执行失败(exit={code}): {out.strip()[:200]}"
        with _gpu_lock:
            _gpu_cache.update(ts=now, data=result)
        return result

    fields = _NVIDIA_SMI_QUERY.split(",")
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(fields):
            continue
        row = dict(zip(fields, parts))
        result["gpus"].append(
            {
                "index": int(_num(row["index"]) or 0),
                "name": row["name"],
                "driver": row["driver_version"],
                "util_gpu": _num(row["utilization.gpu"]),
                "util_mem": _num(row["utilization.memory"]),
                "mem_used_mb": _num(row["memory.used"]),
                "mem_total_mb": _num(row["memory.total"]),
                "temp_c": _num(row["temperature.gpu"]),
                "power_w": _num(row["power.draw"]),
                "power_limit_w": _num(row["power.limit"]),
                "fan_pct": _num(row["fan.speed"]),
                "clock_sm_mhz": _num(row["clocks.sm"]),
                "clock_mem_mhz": _num(row["clocks.mem"]),
            }
        )
    result["available"] = bool(result["gpus"])
    if not result["available"]:
        result["reason"] = "nvidia-smi 没有返回任何显卡"

    # 谁在占用显卡（运维排障时最想知道的）
    code, out = _run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ],
        timeout=6.0,
    )
    procs = []
    if code == 0:
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3 and parts[0]:
                procs.append(
                    {
                        "pid": int(_num(parts[0]) or 0),
                        "name": parts[1],
                        "used_memory_mb": _num(parts[2]),
                    }
                )
    result["processes"] = procs

    with _gpu_lock:
        _gpu_cache.update(ts=now, data=result)
    return result


def system_status(target_dir: Path | None = None) -> dict[str, Any]:
    """CPU / 内存 / 磁盘 / 负载：只读 /proc 和 shutil，无额外依赖。"""
    info: dict[str, Any] = {}
    # os.getloadavg() 在 Windows 上不存在（Linux 才有），所以这里必须吞掉所有异常
    try:
        load1, load5, load15 = os.getloadavg()
        info["load"] = {
            "1m": round(load1, 2),
            "5m": round(load5, 2),
            "15m": round(load15, 2),
        }
    except Exception:
        pass
    info["cpu_count"] = os.cpu_count()
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            mem: dict[str, str] = {}
            for line in fh:
                key, _, rest = line.partition(":")
                mem[key.strip()] = rest.strip()
        total = int(mem.get("MemTotal", "0 kB").split()[0]) / 1024
        avail = int(mem.get("MemAvailable", "0 kB").split()[0]) / 1024
        swap_total = int(mem.get("SwapTotal", "0 kB").split()[0]) / 1024
        swap_free = int(mem.get("SwapFree", "0 kB").split()[0]) / 1024
        info["memory"] = {
            "total_mb": round(total),
            "available_mb": round(avail),
            "used_mb": round(total - avail),
            "used_pct": round((total - avail) / total * 100, 1) if total else 0,
            "swap_total_mb": round(swap_total),
            "swap_used_mb": round(swap_total - swap_free),
        }
    except Exception:
        pass
    try:
        usage = shutil.disk_usage(str(target_dir or Path("/")))
        info["disk"] = {
            "path": str(target_dir or "/"),
            "total_gb": round(usage.total / 1024**3, 1),
            "used_gb": round(usage.used / 1024**3, 1),
            "free_gb": round(usage.free / 1024**3, 1),
            "used_pct": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        }
    except Exception:
        pass
    try:
        with open("/proc/uptime", encoding="ascii") as fh:
            info["uptime_seconds"] = round(float(fh.read().split()[0]))
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# 最近请求（面板上的活动列表）
# ---------------------------------------------------------------------------


class RequestLog:
    def __init__(self, limit: int = 50) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=limit)
        self._lock = threading.Lock()

    def add(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._items.appendleft(entry)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)


request_log = RequestLog()


def _load_html() -> str:
    try:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 文件缺失时的兜底
        logger.warning("管理页面文件缺失(%s)，返回最小页面", exc)
        return (
            "<!doctype html><meta charset='utf-8'>"
            "<h1>TTS 服务运行中</h1>"
            "<p>管理页面文件缺失：static/index.html</p>"
            "<p><a href='/api/'>JSON 索引</a> · <a href='/docs'>API 文档</a></p>"
        )


def register_webui(
    app: FastAPI,
    *,
    config: Any,
    engine: Any,
    storage: Any,
    auth_dependency: Callable[..., Any] | None = None,
) -> None:
    """挂载管理面板与状态接口。"""
    secure = [Depends(auth_dependency)] if auth_dependency is not None else []
    html = _load_html()
    webui_auth = WebUIAuth(config)

    @app.middleware("http")
    async def _record_tts_requests(request: Request, call_next):
        path = request.url.path
        if webui_auth.enabled and WebUIAuth.needs_auth(path):
            if not webui_auth.verify(request.cookies.get(COOKIE_NAME)):
                if path.startswith("/api/"):
                    return JSONResponse(
                        {"detail": {"code": "unauthorized", "message": "请先登录控制台。"}},
                        status_code=401,
                    )
                return RedirectResponse("/login", status_code=302)
        tracked = path.startswith("/api/tts") or path == "/api/cleanup"
        started = time.time()
        response = await call_next(request)
        if tracked:
            request_log.add(
                {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
                    "method": request.method,
                    "path": path,
                    "status": response.status_code,
                    "duration": round(time.time() - started, 2),
                    "client": request.client.host if request.client else "",
                }
            )
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def management_page() -> HTMLResponse:
        return HTMLResponse(html)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page() -> Any:
        if not webui_auth.enabled:
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(LOGIN_HTML)

    @app.post("/api/login", include_in_schema=False)
    async def login(payload: LoginRequest, response: Response) -> dict:
        if not webui_auth.enabled:
            return {"success": True, "protected": False}
        if not webui_auth.check_credentials(payload.username, payload.password):
            # 稍微延迟一点，抬高暴力破解成本
            time.sleep(0.6)
            logger.warning("控制台登录失败（用户名 %r）", payload.username[:32])
            raise HTTPException(
                status_code=401,
                detail={"code": "bad_credentials", "message": "用户名或密码不正确。"},
            )
        response.set_cookie(
            COOKIE_NAME,
            webui_auth.issue(),
            max_age=webui_auth.session_seconds,
            httponly=True,
            samesite="lax",
            path="/",
        )
        logger.info("控制台登录成功（用户名 %s）", webui_auth.username)
        return {
            "success": True,
            "protected": True,
            "username": webui_auth.username,
            "expires_in": webui_auth.session_seconds,
        }

    @app.post("/api/logout", include_in_schema=False)
    async def logout(response: Response) -> dict:
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"success": True}

    @app.get("/api/", include_in_schema=False)
    async def json_index() -> dict:
        return {
            "service": "tts-with-rvc service",
            "ui": "/",
            "endpoints": [
                "GET  /",
                "GET  /api/health",
                "GET  /api/gpu",
                "GET  /api/system",
                "GET  /api/stats",
                "GET  /api/config",
                "POST /api/cleanup",
                "POST /api/tts",
                "POST /api/tts/file",
                "GET  /audio/{filename}",
            ],
        }

    @app.get("/api/gpu")
    async def gpu_api() -> dict:
        return {
            "success": True,
            "gpu": gpu_status(),
            "system": system_status(storage.output_dir),
        }

    @app.get("/api/system")
    async def system_api() -> dict:
        return {"success": True, "system": system_status(storage.output_dir)}

    @app.get("/api/stats")
    async def stats_api() -> dict:
        return {
            "success": True,
            "engine": engine.status(),
            "storage": storage.stats(),
            "output_dir": str(storage.output_dir),
            "webui_protected": webui_auth.enabled,
            "recent": request_log.list(),
        }

    @app.get("/api/config", dependencies=secure)
    async def config_api() -> dict:
        return {
            "success": True,
            "config": config.describe(),
            "auth_enabled": config.auth_enabled,
        }

    @app.post("/api/cleanup", dependencies=secure)
    async def cleanup_api() -> dict:
        import asyncio

        cleaned = await asyncio.to_thread(storage.cleanup)
        return {"success": True, "cleaned": cleaned, "storage": storage.stats()}


# ---------------------------------------------------------------------------
# 登录页（同样零外链）
# ---------------------------------------------------------------------------

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · TTS 语音服务</title>
<style>
  :root{--bg:#0d1117;--panel:#161b22;--panel2:#1c232d;--line:#2a323d;
        --fg:#e6edf3;--dim:#8b949e;--accent2:#58a6ff;--err:#f85149}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",Roboto,Helvetica,Arial,sans-serif}
  .card{width:min(380px,92vw);background:var(--panel);border:1px solid var(--line);
        border-radius:14px;padding:26px 26px 22px;box-shadow:0 18px 50px rgba(0,0,0,.45)}
  h1{margin:0 0 4px;font-size:17px;font-weight:600}
  .sub{color:var(--dim);font-size:12px;margin-bottom:18px}
  label{display:block;font-size:12px;color:var(--dim);margin:12px 0 5px}
  input{width:100%;padding:9px 11px;background:var(--panel2);border:1px solid var(--line);
        border-radius:8px;color:var(--fg);font:inherit;outline:none}
  input:focus{border-color:var(--accent2)}
  button{width:100%;margin-top:18px;padding:10px;border-radius:8px;border:1px solid #1f6feb;
         background:#1f6feb;color:#fff;font:inherit;cursor:pointer}
  button:hover{background:#2b7cf0}
  button:disabled{opacity:.55;cursor:not-allowed}
  .msg{margin-top:12px;font-size:12.5px;min-height:18px;color:var(--err)}
  .hint{margin-top:14px;font-size:11.5px;color:var(--dim);
        border-top:1px solid var(--line);padding-top:10px}
  code{background:var(--panel2);border:1px solid var(--line);border-radius:5px;padding:1px 5px;font-size:11.5px}
</style>
</head>
<body>
<form class="card" id="form" autocomplete="on">
  <h1>🎙 TTS 语音服务</h1>
  <div class="sub">请输入控制台账号密码</div>
  <label for="u">用户名</label>
  <input id="u" name="username" autocomplete="username" autofocus>
  <label for="p">密码</label>
  <input id="p" name="password" type="password" autocomplete="current-password">
  <button id="btn" type="submit">登录</button>
  <div class="msg" id="msg"></div>
  <div class="hint">
    账号密码在服务端 <code>config.yaml</code> 的 <code>webui</code> 段配置。
    登录状态保存在本浏览器的 Cookie 里。
  </div>
</form>
<script>
const $ = (id) => document.getElementById(id);
$('form').addEventListener('submit', async function (e) {
  e.preventDefault();
  const btn = $('btn');
  btn.disabled = true;
  $('msg').textContent = '';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: $('u').value, password: $('p').value}),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw new Error((j.detail && j.detail.message) || ('登录失败 HTTP ' + r.status));
    }
    window.location.href = '/';
  } catch (err) {
    $('msg').textContent = err.message;
    btn.disabled = false;
    $('p').select();
  }
});
</script>
</body>
</html>
"""
