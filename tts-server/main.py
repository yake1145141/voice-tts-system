"""AstrBot AI 自动语音回复系统 —— 语音处理端（HTTP API）。

基于 tts-with-rvc（Edge TTS + RVC）对外提供简单的 HTTP 接口：

    GET  /api/health          健康检查（无需鉴权）
    POST /api/tts             文本转语音，返回 JSON（可带 ?format=file 直接返回音频）
    POST /api/tts/file        文本转语音，直接返回音频文件流
    GET  /audio/{filename}    获取已生成的音频文件（用于 audio_url）

鉴权：当 security.api_key 非空时，业务接口必须携带
    Authorization: Bearer <api_key>   或   X-API-Key: <api_key>

启动：
    python main.py
    python main.py --config /path/to/config.yaml --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from config import AppConfig, ConfigError, pretty_number
from engine import TTSEngine, TTSError
from storage import AudioStorage
from webui import register_webui

logger = logging.getLogger("tts_server")


# ---------------------------------------------------------------------------
# 运行环境准备：把包内自带的 bin 目录（ffmpeg 等）加进 PATH
# ---------------------------------------------------------------------------


def prepare_runtime_path() -> str | None:
    """把服务端自带的 bin 目录加入 PATH，返回找到的 ffmpeg 路径。

    为什么需要：tts-with-rvc 内部会直接调用 `ffmpeg` 命令（走 PATH）。
    如果服务不是通过整合包的启动脚本拉起（比如计划任务、系统服务、IDE 调试），
    ffmpeg 就不在 PATH 里，RVC 会报
    `Failed to load audio: [WinError 2] 系统找不到指定的文件`，
    再被上层包装成 `'tuple' object has no attribute 'dtype'`，非常难排查。
    这里在启动时就统一处理好，避免依赖调用方的环境。
    """
    import shutil

    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "bin",   # <bundle>/app/main.py → <bundle>/bin
        here / "bin",          # 与 main.py 同级的 bin
    ]
    for directory in candidates:
        if not directory.is_dir():
            continue
        has_ffmpeg = any(
            (directory / name).exists() for name in ("ffmpeg.exe", "ffmpeg")
        )
        if not has_ffmpeg:
            continue
        current = os.environ.get("PATH", "")
        if str(directory) not in current.split(os.pathsep):
            os.environ["PATH"] = f"{directory}{os.pathsep}{current}"
            logger.info("已将内置目录加入 PATH: %s", directory)
        break

    found = shutil.which("ffmpeg")
    if found:
        logger.info("ffmpeg: %s", found)
    else:
        logger.warning(
            "未找到 ffmpeg！RVC 音色转换需要它。"
            "请把 ffmpeg.exe 放到 <服务目录>/bin/ 下，或安装后加入 PATH。",
        )

    # --- RVC 的本地模型：hubert_base.pt / rmvpe.pt ---
    # tts-with-rvc 是用 os.getcwd() 去找这两个文件的；找不到就会去 HuggingFace
    # 下载（repo: lj1995/VoiceConversionWebUI）。国内机器经常连不上，表现为
    # 启动后卡好几分钟、最后 timeout，看起来像"服务卡死"。
    cwd = Path.cwd()
    bundle_root = None
    for candidate in (here.parent, here):
        if (candidate / "hubert_base.pt").exists():
            bundle_root = candidate
            break
    if bundle_root is not None:
        if not (cwd / "hubert_base.pt").exists():
            try:
                os.chdir(bundle_root)
                logger.info(
                    "已把工作目录切到整合包根目录（供 RVC 查找本地模型）: %s",
                    bundle_root,
                )
            except OSError as exc:  # pragma: no cover - 目录不可写等极端情况
                logger.warning("切换工作目录失败: %s", exc)
        if (Path.cwd() / "hubert_base.pt").exists():
            # 本地已经有模型了，禁止 huggingface_hub 再发联网请求
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            logger.info("已使用本地 RVC 预置模型，跳过 HuggingFace 联网检查")

    return found


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # 抑制第三方库的噪声日志
    for noisy in ("fairseq", "numba", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class TTSRequest(BaseModel):
    text: str = Field(..., description="需要合成的文本（只放可朗读文本，括号内容请在插件端过滤）")


class TTSResponse(BaseModel):
    success: bool = True
    audio_url: str
    audio_path: str
    filename: str
    format: str
    size_bytes: int
    expire_minutes: float
    cached: bool = False


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------


def build_app(config: AppConfig) -> FastAPI:
    storage = AudioStorage(
        output_dir=config.output_dir,
        expire_minutes=config.expire_minutes,
    )
    engine = TTSEngine(config, storage)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.output_dir.mkdir(parents=True, exist_ok=True)
        storage.ensure_dirs()
        logger.info("语音处理端启动中… 配置: %s", config.describe())
        # 启动时就做一次过期清理，避免历史文件堆积
        try:
            cleaned = storage.cleanup()
            if cleaned["removed_audio"] or cleaned["removed_work"]:
                logger.info("启动清理: %s", cleaned)
        except Exception:
            logger.exception("启动清理失败")
        await storage.start_cleanup_loop(
            config.storage.float("cleanup_interval_minutes", 30),
        )
        await engine.start()
        app.state.config = config
        app.state.storage = storage
        app.state.engine = engine
        try:
            yield
        finally:
            await storage.stop_cleanup_loop()
            await engine.shutdown()
            logger.info("语音处理端已停止。")

    app = FastAPI(
        title="TTS-with-RVC Service",
        description="Edge TTS (zh-CN-YunxiNeural) + RVC 语音合成服务，供 AstrBot 插件调用。",
        version="1.0.0",
        lifespan=lifespan,
    )

    # ---------------- 鉴权 ----------------

    def extract_token(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if auth:
            return auth.strip()
        header_key = request.headers.get("x-api-key", "").strip()
        if header_key:
            return header_key
        # 浏览器的 <audio>/<a> 标签没法自定义请求头，管理页面用 ?key= 传一次
        return request.query_params.get("key", "").strip()

    async def require_api_key(request: Request) -> None:
        if not config.auth_enabled:
            return
        if extract_token(request) != config.api_key:
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "API Key 无效或缺失。"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    # ---------------- 错误响应 ----------------

    @app.exception_handler(TTSError)
    async def tts_error_handler(_request: Request, exc: TTSError) -> JSONResponse:
        logger.warning("TTS 失败 [%s]: %s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            error = detail
        else:
            error = {"code": f"http_{exc.status_code}", "message": str(detail)}
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "error": error},
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": {"code": "internal_error", "message": f"服务器内部错误: {exc}"},
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": {
                    "code": "invalid_request",
                    "message": "请求体格式错误，需要 JSON: {\"text\": \"要合成的文本\"}",
                    "detail": exc.errors()[:3] if hasattr(exc, "errors") else None,
                },
            },
        )

    # ---------------- 工具函数 ----------------

    def build_audio_url(request: Request, filename: str) -> str:
        base = config.server.str("public_base_url", "").strip().rstrip("/")
        if not base:
            base = str(request.base_url).rstrip("/")
        return f"{base}/audio/{filename}"

    def file_headers(path: Path, cached: bool) -> dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "X-Audio-Cached": "1" if cached else "0",
            "X-Audio-Expire-Minutes": str(pretty_number(config.expire_minutes)),
        }

    # ---------------- 路由 ----------------

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "success": True,
            "status": "ok" if engine.ready else "degraded",
            "engine": engine.status(),
            "storage": storage.stats(),
            "auth_enabled": config.auth_enabled,
        }

    @app.post("/api/tts", dependencies=[Depends(require_api_key)])
    async def tts_api(request: Request, payload: TTSRequest, format: str | None = None):
        """文本转语音。

        - 默认返回 JSON: ``{"success": true, "audio_url": "/audio/xxxxxxxx.wav"}``
        - ``?format=file``（或 ``?format=audio``）时直接返回音频文件流。
        """
        text = engine.validate_text(payload.text)
        filename = f"{engine.cache_key(text)}.{config.output_format}"
        cached_path = storage.resolve(filename)
        existed = bool(cached_path and storage.is_fresh(cached_path))

        path = await engine.synthesize(text)

        if format and format.lower() in ("file", "audio", "binary", "wav", "mp3"):
            media_type = "audio/mpeg" if path.suffix == ".mp3" else "audio/wav"
            return FileResponse(
                path,
                media_type=media_type,
                filename=path.name,
                headers=file_headers(path, existed),
            )

        return TTSResponse(
            success=True,
            audio_url=build_audio_url(request, path.name),
            audio_path=f"/audio/{path.name}",
            filename=path.name,
            format=path.suffix.lstrip("."),
            size_bytes=path.stat().st_size,
            expire_minutes=pretty_number(config.expire_minutes),
            cached=existed,
        )

    @app.post("/api/tts/file", dependencies=[Depends(require_api_key)])
    async def tts_file(request: Request, payload: TTSRequest) -> FileResponse:
        """文本转语音，直接返回音频文件流（AstrBot 插件默认使用该接口）。"""
        text = engine.validate_text(payload.text)
        filename = f"{engine.cache_key(text)}.{config.output_format}"
        cached_path = storage.resolve(filename)
        existed = bool(cached_path and storage.is_fresh(cached_path))

        path = await engine.synthesize(text)
        media_type = "audio/mpeg" if path.suffix == ".mp3" else "audio/wav"
        headers = file_headers(path, existed)
        headers["X-Audio-Url"] = build_audio_url(request, path.name)
        headers["X-Audio-Filename"] = path.name
        return FileResponse(path, media_type=media_type, filename=path.name, headers=headers)

    @app.get("/audio/{filename}")
    async def get_audio(request: Request, filename: str) -> FileResponse:
        if config.security.bool("protect_audio", False) and config.auth_enabled:
            if extract_token(request) != config.api_key:
                raise HTTPException(
                    status_code=401,
                    detail={"code": "unauthorized", "message": "无权访问该音频。"},
                )

        path = storage.resolve(filename)
        if path is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "audio_not_found", "message": f"音频不存在或已过期: {filename}"},
            )
        if not storage.is_fresh(path):
            try:
                path.unlink()
            except OSError:
                pass
            raise HTTPException(
                status_code=410,
                detail={"code": "audio_expired", "message": "音频已过期，请重新生成。"},
            )

        media_type = "audio/mpeg" if path.suffix == ".mp3" else "audio/wav"
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    # 网页管理面板 + 显卡/主机状态接口（在路由都注册完之后挂载）
    register_webui(
        app,
        config=config,
        engine=engine,
        storage=storage,
        auth_dependency=require_api_key,
    )

    return app


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TTS-with-RVC HTTP 服务")
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("TTS_SERVER_CONFIG"),
        help="配置文件路径（默认: 与本脚本同目录的 config.yaml）",
    )
    parser.add_argument("--host", help="覆盖 server.host")
    parser.add_argument("--port", type=int, help="覆盖 server.port")
    parser.add_argument("--api-key", help="覆盖 security.api_key")
    parser.add_argument("--log-level", help="覆盖 server.log_level")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="只校验配置与依赖，不启动服务",
    )
    return parser.parse_args(argv)


def load_config_from_args(args: argparse.Namespace) -> AppConfig:
    config = AppConfig.load(args.config)
    if args.host:
        config.server["host"] = args.host
    if args.port:
        config.server["port"] = args.port
    if args.api_key:
        config.security["api_key"] = args.api_key
        config.api_key = args.api_key
    if args.log_level:
        config.server["log_level"] = args.log_level
    return config


def create_app(config_path: str | None = None) -> FastAPI:
    """应用工厂，供 ``uvicorn main:create_app --factory`` 使用。

    例：
        uvicorn main:create_app --factory --loop asyncio --host 0.0.0.0 --port 8080
    配置路径可用环境变量 ``TTS_SERVER_CONFIG`` 指定，默认读取同目录 config.yaml。
    """
    return build_app(AppConfig.load(config_path or os.environ.get("TTS_SERVER_CONFIG")))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config_from_args(args)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.server.str("log_level", "INFO"))
    prepare_runtime_path()

    if args.check_config:
        logger.info("配置校验通过: %s", config.describe())
        return 0

    import uvicorn

    app = build_app(config)
    host = config.server.str("host", "0.0.0.0")
    port = config.server.int("port", 8080)
    logger.info("监听 http://%s:%d", host, port)
    # 使用 asyncio 事件循环：tts-with-rvc 依赖的 nest_asyncio 与 uvloop 不完全兼容
    uvicorn.run(app, host=host, port=port, loop="asyncio", log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
