"""tts-with-rvc 调用封装。

设计要点：
1. ``tts-with-rvc`` 是同步阻塞的（内部会跑 torch + Edge TTS），因此所有推理
   都放到线程池里执行，绝不阻塞 FastAPI 的事件循环。
2. 通过信号量限制并发 + 计数器限制排队长度，避免请求无限堆积。
3. 每个任务都有超时；超时或失败都会抛出带明确错误码的 ``TTSError``。
4. ``tts_with_rvc`` 只在工作线程中懒加载，避免它的 ``nest_asyncio.apply()``
   影响 uvicorn 的事件循环。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import random
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any

from config import AppConfig
from storage import AudioStorage

logger = logging.getLogger("tts_server.engine")


# RVC 推理的显存峰值与**单次输入的音频长度**近似线性相关（实测约 15MB/字），
# 与文本总长度无关。所以长文本切成多段分别合成再拼起来，就能让 2GB 显存的小卡
# 也能处理任意长度的文本。
_SENTENCE_ENDS = "。！？!?；;…\n\r"
_SOFT_BREAKS = "，、,：:）)】」』》> "


def _split_keep(text: str, seps: str) -> list[str]:
    """按分隔符切分，并把分隔符留在前一段末尾（保留标点）。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in seps:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def split_text_for_chunks(text: str, limit: int, min_chars: int = 10) -> list[str]:
    """把长文本切成每段不超过 limit 个字符的片段，优先在句子边界切。

    切法分三步：
      1. 先按句末标点（。！？；…）切句；
      2. 单句仍然超长的，退一步按逗号/顿号切，再不行就硬切；
      3. 把过短的片段并回上一段，避免出现一堆半秒的碎音频。
    """
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []
    min_chars = max(0, min_chars)

    pieces: list[str] = []
    for sentence in _split_keep(text, _SENTENCE_ENDS):
        if not sentence:
            continue
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        cur = ""
        for part in _split_keep(sentence, _SOFT_BREAKS):
            if cur and len(cur) + len(part) > limit:
                pieces.append(cur)
                cur = ""
            # 连软标点都没有的长串，直接按长度硬切
            while len(part) > limit:
                pieces.append(part[:limit])
                part = part[limit:]
            cur += part
        if cur:
            pieces.append(cur)

    merged: list[str] = []
    for piece in pieces:
        # 纯标点碎片（例如被软标点切出来的孤零零一个"。"）直接并回上一段，
        # 否则会生成一段没有内容的音频
        if merged and not any(ch.isalnum() for ch in piece):
            merged[-1] += piece
            continue
        if (
            merged
            and len(merged[-1]) < min_chars
            and len(merged[-1]) + len(piece) <= limit
        ):
            merged[-1] += piece
        else:
            merged.append(piece)
    return [m for m in merged if m.strip()]


def cuda_arch_supports(major: int, minor: int, arch_list: list[str]) -> bool:
    """判断当前 PyTorch 能否为 compute capability X.Y 的显卡提供内核。

    规则来自 CUDA 的**二进制兼容性**：为 X.y 编译的 cubin 可以在 X.z（z >= y）上运行。
    所以：
    * P106-100 / P4（sm_61）配 cu121 的 torch（列表里有 sm_50、sm_60）→ **能跑**，
      不需要 sm_61 精确出现在列表里，会复用 sm_60 的内核；
    * 同一张卡配 cu124/cu128 的 torch（列表只有 sm_75 及以上）→ **不能跑**，
      这正是 `no kernel image is available` / `CUDA error: operation not supported` 的来源。
    """
    arches = [a for a in arch_list if a.startswith("sm_")]
    if not arches:
        # 拿不到列表时不要卡住用户，交给运行时错误兜底
        return True
    if f"sm_{major}{minor}" in arches:
        return True
    for arch in arches:
        digits = arch[3:]
        if not digits.isdigit() or len(digits) < 2:
            continue
        arch_major, arch_minor = int(digits[:-1]), int(digits[-1])
        if arch_major == major and arch_minor <= minor:
            return True
    return False


def best_matching_arch(major: int, minor: int, arch_list: list[str]) -> str | None:
    """返回实际会被用上的内核架构（精确命中返回本身，否则返回同大版本里最高的那个）。"""
    exact = f"sm_{major}{minor}"
    if exact in arch_list:
        return exact
    best: tuple[int, str] | None = None
    for arch in arch_list:
        digits = arch[3:] if arch.startswith("sm_") else ""
        if not digits.isdigit() or len(digits) < 2:
            continue
        arch_major, arch_minor = int(digits[:-1]), int(digits[-1])
        if arch_major == major and arch_minor <= minor:
            if best is None or arch_minor > best[0]:
                best = (arch_minor, arch)
    return best[1] if best else None


def install_edge_tts_timeout(timeout: float) -> bool:
    """给 ``edge_tts.Communicate.save`` 套一层超时。返回是否打了补丁。

    为什么必须这么做：

    ``tts-with-rvc`` 内部的调用链是

        speech()  ->  await tts_communicate()
                            ->  await communicate.save(path)     # edge-tts

    而 **edge-tts 这个库本身完全没有任何超时设置**。微软的接口一旦接受了
    WebSocket 连接却不回音频，这个 await 就永远挂着 —— 不抛异常、不返回、
    也不释放 ``can_speak``，整条推理链就此死亡。

    现场证据：2026-09-19 06:22 之后，每一个请求都卡满 180s 超时；
    py-spy 抓到的栈停在 ``tts_with_rvc/inference.py:144`` 的 ``result()``，
    工作线程在事件循环里 select 空转。

    加上超时之后，这种情况会变成 TimeoutError，上层按「可重试错误」处理
    （重试几次，仍失败就按 tts.source 自动切本地离线语音）。
    """
    try:
        import edge_tts
    except ImportError:
        return False

    original = getattr(edge_tts.Communicate, "save", None)
    if original is None:
        return False
    if getattr(original, "_tts_server_timeout_patched", False):
        return True

    async def save_with_timeout(self, path, *args, **kwargs):  # type: ignore[no-untyped-def]
        return await asyncio.wait_for(
            original(self, path, *args, **kwargs),
            timeout=timeout,
        )

    save_with_timeout._tts_server_timeout_patched = True  # type: ignore[attr-defined]
    save_with_timeout._tts_server_original = original  # type: ignore[attr-defined]
    edge_tts.Communicate.save = save_with_timeout  # type: ignore[method-assign]
    logger.info("已给 edge-tts 加上 %.0fs 超时（否则卡住会拖死整个服务）", timeout)
    return True


class TTSError(Exception):
    """带错误码的 TTS 异常，便于 HTTP 层映射成明确的错误响应。"""

    def __init__(self, code: str, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class TTSEngine:
    """把文本转换成音频文件。"""

    def __init__(self, config: AppConfig, storage: AudioStorage) -> None:
        self.config = config
        self.storage = storage
        self.max_concurrent = max(1, config.queue.int("max_concurrent", 2))
        self.max_queue_size = max(0, config.queue.int("max_queue_size", 32))
        self.timeout = float(config.queue.float("timeout", 120))
        self.cache_by_text = config.storage.bool("cache_by_text", True)
        self.max_text_length = config.storage.int("max_text_length", 2000)

        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrent,
            thread_name_prefix="tts-worker",
        )
        # tts-with-rvc 内部使用全局 VC 实例 + 全局 can_speak 标志，并不是线程安全的：
        # 并发调用会导致共享模型被污染（报 'tuple' object has no attribute 'dtype'），
        # 且异常路径不会复位 can_speak，后续请求会永久卡死。这里用进程内锁串行化。
        self._rvc_lock = threading.Lock()
        self._semaphore: asyncio.Semaphore | None = None
        self._init_lock = threading.Lock()
        self._tts: Any = None
        self._device = "cpu"
        self._is_half = False
        self._force_cpu = False
        self.device_fallback = False
        self._gpu_detail = ""          # 显卡检测说明（用于 /api/health 与日志）
        self._torch_arch_list: list[str] = []
        self.ready = False
        self.last_error: str | None = None
        self._inflight = 0
        self._queued = 0
        self._last_sweep = 0.0
        self._stats: dict[str, Any] = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "timeout": 0,
            "cache_hit": 0,
            "last_success_at": None,
            "last_error": None,
            "last_duration": None,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self.storage.ensure_dirs()
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._executor, self._initialize_sync)
            self.ready = True
            self.last_error = None
            logger.info(
                "TTS 引擎就绪: device=%s is_half=%s speaker=%s rvc=%s",
                self._device,
                self._is_half,
                self.config.speaker,
                self.config.rvc.bool("enabled", True),
            )
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("TTS 引擎初始化失败: %s", exc)
        if self.config.rvc.bool("preload", True) and self.ready:
            try:
                await self.warmup()
            except Exception as exc:
                logger.warning("预热失败（不影响启动，首次请求可能较慢）: %s", exc)

    async def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _initialize_sync(self) -> None:
        """在工作线程中加载 tts-with-rvc（同步阻塞）。"""
        with self._init_lock:
            if self._tts is not None:
                return

            # 关键：tts-with-rvc 内部是 await edge_tts 的 save()，而 edge-tts
            # 自己没有超时。不补这一刀，微软接口一旦"接了连接不回音频"，
            # 整个服务就会被一个卡死的 await 拖垮（实测 2026-09-19 发生过）。
            install_edge_tts_timeout(self.config.tts.float("edge_timeout", 60))

            if not self.config.rvc.bool("enabled", True):
                # 调试模式：跳过 RVC，只做 TTS
                if self.tts_source() not in ("sapi", "espeak"):
                    # 提前验证在线语音依赖，但**不阻断启动**：
                    # 少一个可选依赖就让整个引擎起不来太粗暴，而且会让
                    # 「没装 edge-tts 的环境」无法跑单元测试。
                    # 真到请求时再报明确错误即可。
                    try:
                        import edge_tts  # noqa: F401
                    except ImportError as exc:  # pragma: no cover - 取决于环境
                        logger.warning(
                            "未安装 edge-tts（%s）：在线语音不可用。"
                            "Windows 可把 tts.source 设为 sapi，"
                            "Linux 可安装 espeak-ng 并设为 auto/espeak 走离线兜底。",
                            exc,
                        )

                self._device = self._resolve_device()
                self._is_half = False
                logger.warning(
                    "rvc.enabled = false：当前为【调试模式】，只输出 TTS 原始音频，"
                    "不会做 RVC 音色转换。",
                )
                return

            from tts_with_rvc import TTS_RVC  # 重量级导入，放在工作线程中执行

            self._device = self._resolve_device()
            self._is_half = self.config.rvc.bool("is_half", True) and self._device != "cpu"

            model_path = self.config.model_path
            index_path = self.config.index_path
            self._tts = TTS_RVC(
                model_path=str(model_path),
                index_path=index_path,
                f0_method=self.config.f0_method,
                device=self._device,
                voice=self.config.speaker,
                output_directory=str(self.storage.work_dir),
                tmp_directory=str(self.storage.work_dir),
            )
            self._tts.set_voice(self.config.speaker)
            logger.info(
                "已加载 tts-with-rvc: model=%s index=%s f0=%s device=%s",
                model_path,
                index_path or "(未配置)",
                self.config.f0_method,
                self._device,
            )

    def _check_cuda_supported(self) -> tuple[bool, str]:
        """检查当前 PyTorch 是否支持这张显卡（架构 / 显存）。

        返回 (是否可用, 说明)。常见不可用原因：
        * 老显卡计算能力不在 PyTorch 支持列表（例如 Pascal sm_61 遇上只编译了 sm_75+ 的
          cu128 版本）——报错形如 CUDA error: operation not supported / no kernel image；
        * vGPU 1Q 档位只有 1GB 显存，而 RVC 需要 2~3GB。
        """
        try:
            import torch

            if not torch.cuda.is_available():
                return False, "CUDA 不可用（未装驱动或没有 NVIDIA 显卡）"

            name = torch.cuda.get_device_name(0)
            major, minor = torch.cuda.get_device_capability(0)
            cc = f"sm_{major}{minor}"
            arch_list: list[str] = []
            try:
                arch_list = [a for a in torch.cuda.get_arch_list() if a.startswith("sm_")]
            except Exception:
                pass
            if arch_list and not cuda_arch_supports(major, minor, arch_list):
                return False, (
                    f"{name}（计算能力 {cc}）不在当前 PyTorch 支持的架构列表 "
                    f"[{', '.join(arch_list)}] 内"
                )
            matched = best_matching_arch(major, minor, arch_list) if arch_list else None
            if matched and matched != cc:
                # 同大版本低算力内核（CUDA 二进制兼容），例如 sm_61 显卡复用 sm_60 内核
                cc = f"{cc}（复用 {matched} 内核）"

            total_mb = torch.cuda.get_device_properties(0).total_memory / 1024 / 1024
            min_mb = self.config.rvc.float("min_vram_mb", 2500)
            # 注意：vGPU（如 GRID P40-1Q）上报的显存可能是整卡容量而非切片容量，
            # 所以这里只是"提前劝退"，真正兜底靠运行时的错误降级。
            if total_mb < min_mb:
                return False, f"显存 {total_mb:.0f} MB 低于 rvc.min_vram_mb={min_mb:.0f}"
            return True, cc
        except Exception as exc:  # pragma: no cover
            return False, f"检测显卡失败: {exc}"

    def _resolve_device(self) -> str:
        """决定用哪块设备。

        - `rvc.device` 明确写了 cpu / cuda:0 时按其执行；
        - 写了 auto 时：只有 CUDA 可用**且这张卡被当前 PyTorch 支持**（架构 + 显存）才用 GPU，
          否则直接落到 CPU 并给出明确日志；
        - 一旦运行中降级过（_force_cpu），后续都用 CPU。
        """
        if self._force_cpu:
            return "cpu"
        configured = self.config.device
        if configured and configured != "auto":
            return configured
        try:
            usable, detail = self._check_cuda_supported()
            self._gpu_detail = detail
            try:
                import torch

                self._torch_arch_list = [a for a in torch.cuda.get_arch_list() if a.startswith("sm_")]
            except Exception:
                self._torch_arch_list = []
            if not usable:
                logger.warning(
                    "自动模式检测到显卡不可用：%s。已改用 CPU 推理"
                    "（老显卡请换用 cu121/cu118 版 PyTorch，或把 rvc.device 设为 cuda:0 强制尝试）；"
                    "如需 GPU 请确认显存 ≥ 4GB。",
                    detail,
                )
                self._force_cpu = True
                return "cpu"
            return "cuda:0"
        except Exception as exc:  # pragma: no cover - torch 未安装时回退 CPU
            logger.debug("检测 CUDA 失败，使用 CPU: %s", exc)
            return "cpu"

    # ------------------------------------------------------------------
    # 显存不足时的自动降级
    # ------------------------------------------------------------------

    # GPU 不可用的典型报错（既有显存不足，也有老卡架构不被支持）
    _GPU_UNUSABLE_MARKERS = (
        "out of memory",                     # 显存不足
        "cuda_error_out_of_memory",
        "operation not supported",           # 老架构内核跑不了（P40/sm_61 等）
        "no kernel image is available",      # 同上
        "not compatible with the current pytorch installation",
        "invalid device function",           # 架构不匹配
        "device-side assert",
        "cuda driver version is insufficient",
    )

    @classmethod
    def _is_gpu_unusable(cls, exc: BaseException) -> bool:
        """判断异常是否意味着"这张卡用不了"（显存不足或架构不被支持）。"""
        text = str(exc).lower()
        if any(marker in text for marker in cls._GPU_UNUSABLE_MARKERS):
            return True
        try:
            import torch

            oom_type = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
            if oom_type is not None and isinstance(exc, oom_type):
                return True
        except Exception:
            pass
        return False

    def _fallback_to_cpu(self, reason: str) -> None:
        """把引擎永久切换到 CPU，并重建推理实例（显卡不可用时的兜底）。"""
        if self._device == "cpu":
            return
        logger.warning(
            "显卡不可用（%s），自动切换到 CPU 推理；本次及后续请求都会用 CPU。"
            "常见原因：显存不足（RVC 需要 2~3GB）、显卡架构不在 PyTorch 支持列表"
            "（老卡请用 cu121/cu118 版）、vGPU 切片显存太小。",
            reason[:200],
        )
        self.device_fallback = True
        self._force_cpu = True
        self._device = "cpu"
        self._is_half = False
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        # 重建实例（_initialize_sync 会按 _force_cpu 走 CPU 分支）
        try:
            self._tts = None
            self._initialize_sync()
            logger.info("已用 CPU 重新加载模型，可继续合成。")
        except Exception as exc:  # pragma: no cover - 极端情况
            logger.error("切换到 CPU 后重新加载模型失败: %s", exc)

    async def warmup(self) -> None:
        text = "语音服务已就绪。"
        logger.info("开始预热 TTS 引擎……")
        path = await self.synthesize(text, use_cache=False)
        logger.info("预热完成，测试音频: %s", path)

    # ------------------------------------------------------------------
    # 合成
    # ------------------------------------------------------------------

    def validate_text(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise TTSError("empty_text", "text 不能为空。", status_code=400)
        if len(text) > self.max_text_length:
            raise TTSError(
                "text_too_long",
                f"文本长度 {len(text)} 超过服务端上限 {self.max_text_length}。",
                status_code=413,
            )
        return text

    def cache_key(self, text: str) -> str:
        payload = "\x1f".join(
            [
                text,
                self.config.speaker,
                str(self.config.tts.int("pitch", 0)),
                str(self.config.tts.int("edge_pitch_hz", 0)),
                str(self.config.tts.int("rate", 0)),
                str(self.config.tts.int("volume", 0)),
                self.config.f0_method,
                self.config.rvc_model_name,
                str(self.config.rvc.get("index_rate", 0.75)),
                self.config.output_format,
            ],
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    async def synthesize(self, text: str, *, use_cache: bool = True) -> Path:
        """把文本合成为音频文件，返回最终文件路径。"""
        text = self.validate_text(text)
        if not self.ready:
            raise TTSError(
                "engine_not_ready",
                self.last_error or "TTS 引擎尚未就绪。",
                status_code=503,
            )

        # 顺带做一次过期清理（最多每分钟一次），保证音频到点即删
        await self._maybe_sweep()

        digest = self.cache_key(text)
        filename = f"{digest}.{self.config.output_format}"

        if use_cache and self.cache_by_text:
            cached = self.storage.resolve(filename)
            if cached is not None and self.storage.is_fresh(cached):
                self.storage.touch(cached)
                self._stats["cache_hit"] += 1
                logger.debug("命中缓存: %s", cached.name)
                return cached

        if self.max_queue_size and self._queued >= self.max_queue_size:
            raise TTSError(
                "queue_full",
                f"TTS 队列已满（等待中 {self._queued}，上限 {self.max_queue_size}），请稍后重试。",
                status_code=503,
            )

        assert self._semaphore is not None
        self._queued += 1
        self._stats["total"] += 1
        started = time.time()
        admitted = False
        try:
            async with self._semaphore:
                admitted = True
                self._queued -= 1
                self._inflight += 1
                try:
                    loop = asyncio.get_running_loop()
                    future = loop.run_in_executor(
                        self._executor,
                        self._synthesize_sync,
                        text,
                        filename,
                    )
                    try:
                        # 长文本会分成多段串行推理，按段数放大超时，避免误判超时
                        effective_timeout = self.timeout + 30.0 * (self.chunk_plan(text) - 1)
                        result = await asyncio.wait_for(
                            asyncio.shield(future),
                            timeout=effective_timeout,
                        )
                    except asyncio.TimeoutError:
                        self._stats["timeout"] += 1
                        future.add_done_callback(self._log_late_failure)
                        raise TTSError(
                            "timeout",
                            f"TTS 处理超时（>{effective_timeout:.0f}s），"
                            "请稍后重试、减小文本长度，或调大 queue.timeout。",
                            status_code=504,
                        ) from None
                finally:
                    self._inflight -= 1
        except TTSError:
            self._stats["failed"] += 1
            raise
        except Exception as exc:
            self._stats["failed"] += 1
            raise TTSError("rvc_failed", f"音频生成失败: {exc}", status_code=500) from exc
        finally:
            if not admitted:
                # 在排队阶段就被取消/异常，需要把排队计数还原
                self._queued = max(0, self._queued - 1)

        self._stats["success"] += 1
        self._stats["last_success_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._stats["last_duration"] = round(time.time() - started, 2)
        self._stats["last_error"] = None
        logger.info(
            "生成完成: %s (%.2fs, %d 字)",
            result.name,
            time.time() - started,
            len(text),
        )
        return result

    async def _maybe_sweep(self) -> None:
        """廉价的过期清理：即使后台任务间隔较长，也能让文件尽快被删除。"""
        now = time.time()
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        try:
            await asyncio.to_thread(self.storage.cleanup)
        except Exception:  # pragma: no cover - 清理失败不影响本次合成
            logger.warning("顺带清理过期音频失败", exc_info=True)

    @staticmethod
    def _log_late_failure(future) -> None:
        try:
            future.result()
        except Exception as exc:  # pragma: no cover - 仅记录超时后残留的错误
            logger.warning("超时任务最终失败: %s", exc)

    # ------------------------------------------------------------------
    # 真正干活的同步函数（运行在线程池中）
    # ------------------------------------------------------------------

    def _synthesize_sync(self, text: str, filename: str) -> Path:
        chunks = self.chunk_text(text)
        if len(chunks) <= 1:
            raw_path = self._synthesize_once(text, filename)
        else:
            logger.info(
                "长文本分段合成：%d 字 → %d 段（每段 ≤%d 字，显存峰值只与单段长度有关）",
                len(text),
                len(chunks),
                self.chunk_max_chars,
            )
            parts: list[Path] = []
            try:
                for index, chunk in enumerate(chunks, 1):
                    parts.append(
                        self._synthesize_once(chunk, f"part{index:02d}-{filename}")
                    )
                raw_path = self._concat_audio(parts)
            finally:
                for part in parts:
                    try:
                        if part.exists():
                            part.unlink()
                    except OSError:
                        pass

        if not raw_path.exists() or raw_path.stat().st_size < 512:
            raise TTSError(
                "empty_audio",
                f"生成的音频文件为空或损坏: {raw_path}",
                status_code=500,
            )

        final_path = self._post_process(raw_path, filename)
        if not final_path.exists():
            raise TTSError("empty_audio", "音频后处理失败。", status_code=500)
        # 清理中间产物
        for candidate in {raw_path, raw_path.with_suffix(".mp3")}:
            try:
                if candidate.exists() and candidate != final_path:
                    candidate.unlink()
            except OSError:
                pass
        return final_path

    # ------------------------------------------------------------------
    # 长文本分段
    # ------------------------------------------------------------------

    @property
    def chunk_enabled(self) -> bool:
        return self.config.chunk.bool("enabled", True)

    @property
    def chunk_max_chars(self) -> int:
        return max(0, self.config.chunk.int("max_chars", 80))

    def chunk_text(self, text: str) -> list[str]:
        """按配置把文本分成若干段；分段关闭或文本够短时返回单段。"""
        text = (text or "").strip()
        if not text:
            return []
        limit = self.chunk_max_chars
        if not self.chunk_enabled or limit <= 0 or len(text) <= limit:
            return [text]
        return split_text_for_chunks(text, limit, self.config.chunk.int("min_chars", 10))

    def chunk_plan(self, text: str) -> int:
        """预估会分成几段（用于自适应超时，不实际切分）。"""
        limit = self.chunk_max_chars
        if not self.chunk_enabled or limit <= 0:
            return 1
        return max(1, (len(text) + limit - 1) // limit)

    def _concat_audio(self, parts: list[Path]) -> Path:
        """用 ffmpeg 把多段音频首尾拼接成一段（无损：输入输出都是 PCM）。"""
        if not parts:
            raise TTSError("empty_audio", "分段合成为空。", status_code=500)
        if len(parts) == 1:
            return parts[0]
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise TTSError(
                "ffmpeg_missing",
                "未找到 ffmpeg，无法拼接分段音频。请安装 ffmpeg 并加入 PATH。",
                status_code=500,
            )
        self.storage.ensure_dirs()
        stamp = int(time.time() * 1000)
        list_file = self.storage.work_dir / f"concat-{stamp}.txt"
        target = self.storage.work_dir / f"merged-{stamp}.wav"
        list_file.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in parts),
            encoding="utf-8",
        )
        try:
            subprocess.run(
                [
                    ffmpeg, "-y", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", str(list_file),
                    "-c:a", "pcm_s16le", str(target),
                ],
                check=True,
                capture_output=True,
                timeout=180,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", errors="ignore")[:200]
            raise TTSError("ffmpeg_failed", f"分段音频拼接失败: {detail}", status_code=500) from exc
        except subprocess.TimeoutExpired as exc:
            raise TTSError("ffmpeg_failed", "分段音频拼接超时。", status_code=500) from exc
        finally:
            try:
                list_file.unlink()
            except OSError:
                pass
        if not target.exists() or target.stat().st_size < 512:
            raise TTSError("empty_audio", "分段音频拼接结果为空。", status_code=500)
        return target

    def _synthesize_once(self, text: str, filename: str) -> Path:
        """单段合成：按顺序尝试各个语音源，返回未后处理的原始音频路径。"""
        backends = self._tts_backends()
        last_exc: BaseException | None = None
        raw_path: Path | None = None

        for index, backend in enumerate(backends):
            try:
                raw_path = self._run_backend(backend, text, filename)
                if index > 0:
                    logger.warning(
                        "已改用本地 TTS（%s）完成本次合成，原因：%s",
                        backend,
                        str(last_exc)[:160] if last_exc else "上一个语音源失败",
                    )
                break
            except TTSError as exc:
                last_exc = exc
                if self._is_gpu_unusable(exc) and self._maybe_fallback(exc):
                    try:
                        raw_path = self._run_backend(backend, text, filename)
                        break
                    except Exception as retry_exc:  # noqa: BLE001
                        last_exc = retry_exc
                if index + 1 >= len(backends):
                    raise
            except Exception as exc:
                last_exc = exc
                if self._is_gpu_unusable(exc) and self._maybe_fallback(exc):
                    try:
                        raw_path = self._run_backend(backend, text, filename)
                        break
                    except Exception as retry_exc:  # noqa: BLE001
                        last_exc = retry_exc
                if index + 1 >= len(backends):
                    self._stats["last_error"] = str(last_exc)
                    raise TTSError(
                        "rvc_failed",
                        f"tts-with-rvc 执行失败: {last_exc}",
                        status_code=500,
                    ) from last_exc

        if raw_path is None:  # pragma: no cover - 理论上不会走到
            raise TTSError("rvc_failed", "所有语音源都失败了。", status_code=500)

        # 只负责产出原始音频；校验、后处理、清理都由 _synthesize_sync 统一处理
        return raw_path

    def _maybe_fallback(self, exc: BaseException) -> bool:
        """显卡不可用时降级到 CPU；返回 True 表示已降级、可以重试。"""
        if self._device == "cpu":
            return False
        if not self.config.rvc.bool("allow_cpu_fallback", True):
            logger.error("显卡不可用，且 rvc.allow_cpu_fallback=false，不再自动降级。")
            return False
        self._fallback_to_cpu(str(exc))
        return self._tts is not None

    # ------------------------------------------------------------------
    # 卡死自愈：库内部线程卡住时无法从 Python 层中断，只能重启进程
    # ------------------------------------------------------------------

    @property
    def hard_timeout(self) -> float:
        """单次库调用的硬上限（秒）。超过就判定卡死，重启进程。

        必须小于 queue.timeout，否则外层先超时、用户只会看到普通超时错误。
        """
        hard = float(self.config.queue.float("hard_timeout", 150))
        hard = max(5.0, hard)
        # 无论配置怎么写，硬超时都必须留在 queue.timeout 之内，
        # 否则外层先超时，用户只能看到普通超时错误，自愈重启永远不会触发。
        if self.timeout > 5:
            hard = min(hard, self.timeout - 5)
        return hard

    def _abort_stuck_process(self, reason: str) -> None:
        """判定进程已卡死，主动退出，交给 systemd 重启。

        库卡在网络的 await 上时，Python 层面没有任何办法中断那个线程
        （asyncio 超时取消了也没用，线程还占着锁）。继续跑下去的唯一结果是
        **后续每一个请求都超时**。直接退出进程、让 systemd 几秒后拉起来，
        是恢复最快、状态最干净的做法。
        """
        self._stats["last_error"] = reason
        self._stats["stuck_restart"] = self._stats.get("stuck_restart", 0) + 1
        logger.error(
            "%s。主动退出进程以便 systemd 重启服务（Restart=always），"
            "否则后续所有请求都会一直超时。",
            reason,
        )
        try:
            logging.shutdown()
        except Exception:  # pragma: no cover
            pass
        # 用 os._exit 跳过各种清理钩子：卡住的线程会阻塞正常的解释器退出
        os._exit(70)

    def _call_library_bounded(self, **kwargs: Any) -> Any:
        """在线程里调用 tts-with-rvc，并加硬超时。

        注意不能用 ``with ThreadPoolExecutor(...)``：它的 ``__exit__`` 是
        ``shutdown(wait=True)``，正好会再等一次那个卡死的线程，等于没加超时。
        所以手动 ``shutdown(wait=False)``；真超时就直接重启进程。
        """
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rvc-call")
        try:
            future = pool.submit(self._tts, **kwargs)
            try:
                return future.result(timeout=self.hard_timeout)
            except FuturesTimeout:
                self._abort_stuck_process(
                    f"tts-with-rvc 调用超过 {self.hard_timeout:.0f}s 未返回"
                    "（多半是 edge-tts 卡在网络等待上）"
                )
            except Exception:
                raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # 语音源（TTS 后端）选择
    # ------------------------------------------------------------------

    def tts_source(self) -> str:
        """当前配置的语音源。

        edgetts = 微软在线语音；sapi = Windows 自带离线语音；
        espeak = Linux/macOS 上的 espeak-ng 离线语音；auto = 先在线，失败依次转本地。
        """
        source = str(self.config.tts.get("source", "edgetts") or "edgetts").strip().lower()
        if source in {"sapi", "sapi5", "windows"}:
            return "sapi"
        if source in {"espeak", "espeak-ng"}:
            return "espeak"
        if source in {"local", "offline"}:
            return "sapi" if os.name == "nt" else "espeak"
        if source in {"auto", "fallback"}:
            return "auto"
        return "edgetts"

    def _tts_backends(self) -> list[str]:
        """按顺序尝试的语音源列表（auto 模式下在线失败会自动转本地离线）。"""
        source = self.tts_source()
        local = [b for b in ("sapi", "espeak") if self._local_tts_available(b)]
        if source == "sapi":
            return ["sapi"] if "sapi" in local else ["edgetts"]
        if source == "espeak":
            return ["espeak"] if "espeak" in local else ["edgetts"]
        if source == "auto":
            return ["edgetts"] + local
        return ["edgetts"]

    def _local_tts_available(self, backend: str) -> bool:
        if backend == "sapi":
            return self._sapi_supported()
        if backend == "espeak":
            return self._find_espeak() is not None
        return False

    def _run_backend(self, backend: str, text: str, filename: str) -> Path:
        if self.config.rvc.bool("enabled", True):
            return self._run_tts_with_rvc(text, filename, backend=backend)
        return self._run_tts_only(text, backend=backend)

    def _run_tts_only(self, text: str, backend: str = "edgetts") -> Path:
        """不使用 RVC 的纯 TTS 输出（rvc.enabled=false 的调试模式）。"""
        if backend == "sapi":
            return self._sapi_synthesize(text)
        if backend == "espeak":
            return self._espeak_synthesize(text)
        return self._run_edge_tts_only(text)

    def _run_tts_with_rvc(self, text: str, filename: str, backend: str = "edgetts") -> Path:
        if self._tts is None:
            raise TTSError("engine_not_ready", "TTS 引擎未初始化。", status_code=503)

        work_name = f"rvc-{filename}"
        retries = max(0, self.config.tts.int("retries", 3))
        if backend == "sapi":
            # 本地 TTS 不依赖网络，瞬时错误通常来自合成器本身，重试 1 次足够
            retries = min(retries, 1)
        # 串行化：同一时刻只允许一个 RVC 推理（库本身也不支持并发）。
        #
        # 这里用「带超时的获取」而不是 `with`：库内部是
        #   self.pool.submit(asyncio.run, speech(...)).result()
        # 而 speech() 第一步就是 await edge_tts 的 save()，**edge-tts 自己没有超时**。
        # 一旦微软那边接了连接却不回音频，这个 await 会永远挂着：
        # 它不发异常、不释放锁，于是后面每个请求都在等锁 → 全部超时。
        # 所以拿不到锁就说明上一次推理已经卡死，必须重启进程才能清掉那个线程。
        lock_wait = self.hard_timeout
        acquired = self._rvc_lock.acquire(timeout=lock_wait)
        if not acquired:
            self._abort_stuck_process(
                f"RVC 推理锁已被占用超过 {lock_wait:.0f}s，上一次推理卡死未释放"
            )
        try:
            attempt = 0
            while True:
                try:
                    if backend == "sapi":
                        output = self._run_sapi_with_rvc(text, work_name)
                    elif backend == "espeak":
                        output = self._run_espeak_with_rvc(text, work_name)
                    else:
                        output = self._call_library_bounded(
                            text=text,
                            pitch=self.config.tts.int("pitch", 0),
                            tts_rate=self.config.tts.int("rate", 0),
                            tts_volume=self.config.tts.int("volume", 0),
                            tts_pitch=self.config.tts.int("edge_pitch_hz", 0),
                            output_filename=work_name,
                            index_rate=self.config.rvc.float("index_rate", 0.75),
                            is_half=self._is_half,
                            f0method=self.config.f0_method,
                            filter_radius=self.config.rvc.int("filter_radius", 3),
                            resample_sr=self.config.rvc.int("resample_sr", 0),
                            rms_mix_rate=self.config.rvc.float("rms_mix_rate", 0.5),
                            protect=self.config.rvc.float("protect", 0.33),
                        )
                    break
                except Exception as exc:
                    # tts-with-rvc 在异常路径不会复位全局 can_speak，必须兜底复位，
                    # 否则后续所有请求都会卡在 `while not can_speak` 里直到重启服务
                    self._reset_library_speak_flag()
                    if attempt < retries and self._is_transient_tts_error(exc):
                        attempt += 1
                        logger.warning(
                            "TTS 临时失败（%s），重试 %d/%d …",
                            str(exc)[:120],
                            attempt,
                            retries,
                        )
                        # 退避 + 抖动：避免与其它请求同时重试再次被上游拒绝
                        time.sleep(0.8 * attempt + random.uniform(0.0, 0.6))
                        continue
                    # 失败后复位库内部状态：下次调用会重新加载模型，避免复用被污染的实例
                    self._recover_library_state()
                    self._stats["last_error"] = str(exc)
                    raise TTSError(
                        "rvc_failed",
                        f"tts-with-rvc 执行失败: {exc}",
                        status_code=500,
                    ) from exc
        finally:
            self._rvc_lock.release()
        return Path(output)

    # Edge TTS 是微软的在线服务，偶发会返回空音频或直接断连；这类错误重试通常就好了
    _TRANSIENT_TTS_MARKERS = (
        "no audio was received",
        "cannot connect to host",
        "cannot connect",
        "connection aborted",
        "connection refused",
        "connection reset",
        "connection error",
        "connection timeout",
        "connect timeout",
        "timeout to host",
        "timed out",
        "timeout",
        "network is unreachable",
        "name or service not known",
        "nodename nor servname",
        "serverdisconnected",
        "websocket",
        "getaddrinfo",
        "timed out",
        "temporarily unavailable",
        "502",
        "503",
        "504",
    )

    @classmethod
    def _is_transient_tts_error(cls, exc: BaseException) -> bool:
        text = str(exc).lower()
        return any(marker in text for marker in cls._TRANSIENT_TTS_MARKERS)

    @staticmethod
    def _reset_library_speak_flag() -> None:
        """把 tts-with-rvc 的全局 can_speak 复位为 True（幂等）。

        注意：这个标志位在 ``tts_with_rvc.inference`` 模块里（``vc_infer`` 没有），
        ``speech()`` 在推理前置 False、成功后置回 True；一旦推理抛异常就永远是
        False，后续所有请求都会卡在 ``while not can_speak`` 直到超时。
        两个模块都复位一遍，兼容不同版本的库布局。
        """
        try:
            from tts_with_rvc import inference as _inference

            _inference.can_speak = True
        except Exception:  # pragma: no cover - 库未加载时忽略
            pass
        try:
            from tts_with_rvc import vc_infer

            vc_infer.can_speak = True
        except Exception:  # pragma: no cover - 库未加载时忽略
            pass

    @classmethod
    def _recover_library_state(cls) -> None:
        """调用失败后的自愈：复位 can_speak 并强制下次重新加载模型。"""
        cls._reset_library_speak_flag()
        try:
            from tts_with_rvc import vc_infer

            vc_infer.last_model_path = ""
        except Exception:  # pragma: no cover
            pass

    def _run_edge_tts_only(self, text: str) -> Path:
        """只用 Edge TTS 输出（不带 RVC），供 _run_tts_only 在调试模式下调用。"""
        import edge_tts

        self.storage.ensure_dirs()

        rate = self.config.tts.int("rate", 0)
        volume = self.config.tts.int("volume", 0)
        retries = max(0, self.config.tts.int("retries", 3))

        async def _save(mp3_path: Path) -> None:
            communicate = edge_tts.Communicate(
                text=text,
                voice=self.config.speaker,
                rate=f"{'+' if rate >= 0 else ''}{rate}%",
                volume=f"{'+' if volume >= 0 else ''}{volume}%",
            )
            await communicate.save(str(mp3_path))

        attempt = 0
        while True:
            mp3_path = self.storage.work_dir / f"edge-{int(time.time() * 1000)}-{attempt}.mp3"
            try:
                asyncio.run(_save(mp3_path))
                if not mp3_path.exists() or mp3_path.stat().st_size == 0:
                    raise RuntimeError("No audio was received. Please verify that your parameters are correct.")
                return mp3_path
            except Exception as exc:
                if attempt < retries and self._is_transient_tts_error(exc):
                    attempt += 1
                    logger.warning(
                        "Edge TTS 临时失败（%s），重试 %d/%d …",
                        str(exc)[:120],
                        attempt,
                        retries,
                    )
                    time.sleep(0.8 * attempt + random.uniform(0.0, 0.6))
                    continue
                if isinstance(exc, TTSError):
                    raise
                self._stats["last_error"] = str(exc)
                raise TTSError(
                    "tts_failed",
                    f"Edge TTS 执行失败: {exc}",
                    status_code=502,
                )

    # ------------------------------------------------------------------
    # 本地 TTS（Windows SAPI5，完全离线）
    # ------------------------------------------------------------------

    _SAPI_SCRIPT = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Volume = 100;"
        "$voice = $env:TTS_SAPI_VOICE;"
        "if (-not $voice) {"
        "  $voice = ($s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' }"
        " | Select-Object -First 1).VoiceInfo.Name"
        "};"
        "if ($voice) { $s.SelectVoice($voice) };"
        "$s.Rate = [int]$env:TTS_SAPI_RATE;"
        "$s.SetOutputToWaveFile($env:TTS_SAPI_OUT);"
        "$text = [IO.File]::ReadAllText($env:TTS_SAPI_TEXT, [Text.Encoding]::UTF8);"
        "$s.Speak($text);"
        "$s.Dispose()"
    )

    def _find_powershell(self) -> str | None:
        cached = getattr(self, "_powershell_path", "")
        if cached:
            return cached or None
        candidate = shutil.which("powershell.exe") or shutil.which("powershell")
        if not candidate:
            fallback = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"),
                "System32",
                "WindowsPowerShell",
                "v1.0",
                "powershell.exe",
            )
            if os.path.exists(fallback):
                candidate = fallback
        self._powershell_path = candidate or ""
        return candidate

    def _sapi_supported(self) -> bool:
        if os.name != "nt":
            return False
        return self._find_powershell() is not None

    def _sapi_synthesize(self, text: str) -> Path:
        """用 Windows 自带语音（SAPI5）离线合成一段 wav。"""
        if not self._sapi_supported():
            raise TTSError(
                "sapi_unavailable",
                "本地 TTS 不可用：当前系统没有 Windows PowerShell / SAPI5（Linux 上请用 edgetts）。",
                status_code=500,
            )

        self.storage.ensure_dirs()
        stamp = f"{int(time.time() * 1000)}-{os.getpid()}"
        text_file = self.storage.work_dir / f"sapi-{stamp}.txt"
        out_file = self.storage.work_dir / f"sapi-{stamp}.wav"
        text_file.write_text(text, encoding="utf-8")

        encoded = base64.b64encode(self._SAPI_SCRIPT.encode("utf-16-le")).decode("ascii")
        env = dict(os.environ)
        env.update(
            {
                "TTS_SAPI_TEXT": str(text_file),
                "TTS_SAPI_OUT": str(out_file),
                "TTS_SAPI_VOICE": str(self.config.tts.get("sapi_voice", "") or ""),
                "TTS_SAPI_RATE": str(self.config.tts.int("sapi_rate", 0)),
            }
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                [
                    self._find_powershell() or "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                capture_output=True,
                timeout=float(self.config.tts.float("sapi_timeout", 60)),
                env=env,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise TTSError("sapi_timeout", "本地 TTS 合成超时。", status_code=504) from exc
        finally:
            try:
                text_file.unlink()
            except OSError:
                pass

        if proc.returncode != 0 or not out_file.exists() or out_file.stat().st_size < 512:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
            raise TTSError(
                "sapi_failed",
                f"本地 TTS 合成失败（返回码 {proc.returncode}）{('：' + detail) if detail else ''}",
                status_code=500,
            )
        return out_file

    # ------------------------------------------------------------------
    # 本地 TTS（Linux / macOS：espeak-ng，完全离线）
    # ------------------------------------------------------------------

    def _find_espeak(self) -> str | None:
        cached = getattr(self, "_espeak_path", "")
        if cached:
            return cached or None
        candidate = shutil.which("espeak-ng") or shutil.which("espeak")
        self._espeak_path = candidate or ""
        return candidate

    def _espeak_synthesize(self, text: str) -> Path:
        """用 espeak-ng 离线合成一段 wav（音色一般，但经 RVC 转换后可用）。

        Windows 上有系统自带的 SAPI，所以这条路径主要给 Linux / macOS 用：
        断网、微软在线语音被墙或抽风时，仍然能出声。
        """
        exe = self._find_espeak()
        if not exe:
            raise TTSError(
                "espeak_unavailable",
                "本地 TTS 不可用：没有找到 espeak-ng。"
                "Ubuntu/Debian 可执行 sudo apt install -y espeak-ng；"
                "Windows 请把 tts.source 改成 auto 或 sapi。",
                status_code=500,
            )

        self.storage.ensure_dirs()
        stamp = f"{int(time.time() * 1000)}-{os.getpid()}"
        out_file = self.storage.work_dir / f"espeak-{stamp}.wav"
        voice = self.config.tts.str("espeak_voice", "cmn").strip() or "cmn"
        speed = self.config.tts.int("espeak_speed", 180)
        cmd = [
            exe,
            "-v", voice,
            "-s", str(speed),
            "-w", str(out_file),
            text,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=float(self.config.tts.float("espeak_timeout", 60)),
            )
        except subprocess.TimeoutExpired as exc:
            raise TTSError("espeak_timeout", "本地 TTS（espeak-ng）合成超时。", status_code=504) from exc
        if proc.returncode != 0 or not out_file.exists() or out_file.stat().st_size < 512:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            raise TTSError(
                "espeak_failed",
                f"本地 TTS（espeak-ng）合成失败（返回码 {proc.returncode}）"
                f"{('：' + detail) if detail else ''}",
                status_code=500,
            )
        return out_file

    def _run_espeak_with_rvc(self, text: str, output_name: str) -> str:
        """espeak-ng 合成 → RVC 转成目标音色。"""
        wav_path = self._espeak_synthesize(text)
        try:
            self._reset_library_speak_flag()
            result = self._tts.voiceover_file(
                input_path=str(wav_path),
                pitch=self.config.tts.int("pitch", 0),
                output_directory=str(self.storage.work_dir),
                filename=output_name,
                index_rate=self.config.rvc.float("index_rate", 0.75),
                is_half=self._is_half,
                f0method=self.config.f0_method,
                filter_radius=self.config.rvc.int("filter_radius", 3),
                resample_sr=self.config.rvc.int("resample_sr", 0),
                rms_mix_rate=self.config.rvc.float("rms_mix_rate", 0.5),
                protect=self.config.rvc.float("protect", 0.33),
            )
        finally:
            try:
                wav_path.unlink()
            except OSError:
                pass
        if not result:
            raise TTSError(
                "rvc_failed",
                "RVC 音色转换失败（本地 TTS 已成功，转换步骤返回空）。",
                status_code=500,
            )
        return str(result)

    def _run_sapi_with_rvc(self, text: str, output_name: str) -> str:
        """本地 TTS 合成 → 再用 RVC 转换成目标音色。"""
        wav_path = self._sapi_synthesize(text)
        try:
            self._reset_library_speak_flag()
            result = self._tts.voiceover_file(
                input_path=str(wav_path),
                pitch=self.config.tts.int("pitch", 0),
                output_directory=str(self.storage.work_dir),
                filename=output_name,
                index_rate=self.config.rvc.float("index_rate", 0.75),
                is_half=self._is_half,
                f0method=self.config.f0_method,
                filter_radius=self.config.rvc.int("filter_radius", 3),
                resample_sr=self.config.rvc.int("resample_sr", 0),
                rms_mix_rate=self.config.rvc.float("rms_mix_rate", 0.5),
                protect=self.config.rvc.float("protect", 0.33),
            )
        finally:
            try:
                wav_path.unlink()
            except OSError:
                pass

        if not result:
            raise TTSError(
                "rvc_failed",
                "RVC 音色转换失败（本地 TTS 已成功，转换步骤返回空）。",
                status_code=500,
            )
        return str(result)

    def _post_process(self, raw_path: Path, filename: str) -> Path:
        """把中间音频转成最终格式并落盘。"""
        target_format = self.config.output_format
        suffix = raw_path.suffix.lower()

        if target_format == "wav":
            if suffix == ".wav":
                return self.storage.finalize(raw_path, filename)
            return self.storage.finalize(self._ffmpeg(raw_path, "wav"), filename)

        # mp3
        if suffix == ".mp3":
            return self.storage.finalize(raw_path, filename)
        return self.storage.finalize(self._ffmpeg(raw_path, "mp3"), filename)

    def _ffmpeg(self, source: Path, target_format: str) -> Path:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise TTSError(
                "ffmpeg_missing",
                "未找到 ffmpeg，无法进行音频格式转换。请安装 ffmpeg 并加入 PATH。",
                status_code=500,
            )
        self.storage.ensure_dirs()
        target = self.storage.work_dir / f"conv-{int(time.time() * 1000)}.{target_format}"
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(source)]
        if target_format == "mp3":
            cmd += ["-b:a", self.config.audio.str("mp3_bitrate", "64k"), "-ac", "1"]
        else:
            cmd += ["-ac", "1"]
        cmd.append(str(target))
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", errors="ignore")[:200]
            raise TTSError("ffmpeg_failed", f"音频转换失败: {detail}", status_code=500) from exc
        except subprocess.TimeoutExpired as exc:
            raise TTSError("ffmpeg_failed", "音频转换超时。", status_code=500) from exc
        return target

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "device": self._device,
            "is_half": self._is_half,
            "device_fallback": self.device_fallback,
            "min_vram_mb": self.config.rvc.float("min_vram_mb", 2500),
            "gpu_detail": self._gpu_detail,
            "torch_arch_list": self._torch_arch_list,
            "rvc_enabled": self.config.rvc.bool("enabled", True),
            "tts_source": self.tts_source(),
            "sapi_available": self._sapi_supported(),
            "chunk_enabled": self.chunk_enabled,
            "chunk_max_chars": self.chunk_max_chars,
            "rvc_serialized": True,
            "rvc_busy": self._rvc_lock.locked(),
            "model": str(self.config.model_path) if self.config.model_path else "",
            "speaker": self.config.speaker,
            "pitch": self.config.tts.int("pitch", 0),
            "f0_method": self.config.f0_method,
            "max_concurrent": self.max_concurrent,
            "timeout": self.timeout,
            "inflight": self._inflight,
            "queued": self._queued,
            "last_error": self.last_error,
            "stats": dict(self._stats),
        }
