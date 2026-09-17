"""音频文件管理与过期清理。"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from config import pretty_number

logger = logging.getLogger("tts_server.storage")

# 文件名只允许 32 位十六进制 + 扩展名，用于防止路径穿越
FILENAME_RE = re.compile(r"^[0-9a-fA-F]{32}\.(wav|mp3)$")


class AudioStorage:
    """负责生成音频的落盘、查询与过期清理。"""

    def __init__(
        self,
        output_dir: Path,
        expire_minutes: float = 10.0,
        work_dir_name: str = ".work",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.work_dir = self.output_dir / work_dir_name
        self.expire_minutes = max(0.5, float(expire_minutes))
        """音频保留时长（分钟）。生成后超过该时间就会被后台任务删除。"""
        self.expire_seconds = self.expire_minutes * 60.0
        self._cleanup_task: asyncio.Task | None = None

    # ---------------- 目录 ----------------

    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 写入 ----------------

    def new_work_file(self, suffix: str) -> Path:
        self.ensure_dirs()
        return self.work_dir / f"tmp-{uuid.uuid4().hex}{suffix}"

    def finalize(self, tmp_path: Path, filename: str) -> Path:
        """把工作文件移动到输出目录，返回最终路径。"""
        self.ensure_dirs()
        final_path = self.output_dir / filename
        try:
            tmp_path.replace(final_path)
        except OSError:
            shutil.move(str(tmp_path), str(final_path))
        return final_path

    def save_bytes(self, filename: str, data: bytes) -> Path:
        self.ensure_dirs()
        final_path = self.output_dir / filename
        final_path.write_bytes(data)
        return final_path

    # ---------------- 读取 ----------------

    @staticmethod
    def is_valid_filename(filename: str) -> bool:
        return bool(FILENAME_RE.match(filename or ""))

    def resolve(self, filename: str) -> Path | None:
        """返回可安全读取的文件路径；非法/不存在/过期时返回 None。"""
        if not self.is_valid_filename(filename):
            return None
        path = (self.output_dir / filename).resolve()
        try:
            path.relative_to(self.output_dir.resolve())
        except ValueError:
            return None
        if not path.is_file():
            return None
        return path

    def is_fresh(self, path: Path) -> bool:
        try:
            return (time.time() - path.stat().st_mtime) <= self.expire_seconds
        except OSError:
            return False

    def touch(self, path: Path) -> None:
        """命中缓存时刷新访问时间，避免热点音频被过早清理。"""
        try:
            path.touch(exist_ok=True)
        except OSError:
            pass

    # ---------------- 清理 ----------------

    def cleanup(self) -> dict[str, int]:
        """删除过期音频与残留的工作文件。"""
        now = time.time()
        removed_audio = 0
        removed_work = 0
        freed_bytes = 0

        if self.output_dir.exists():
            for item in self.output_dir.iterdir():
                if not item.is_file() or not FILENAME_RE.match(item.name):
                    continue
                try:
                    stat = item.stat()
                except OSError:
                    continue
                if now - stat.st_mtime > self.expire_seconds:
                    try:
                        item.unlink()
                        removed_audio += 1
                        freed_bytes += stat.st_size
                    except OSError as exc:
                        logger.warning("清理音频 %s 失败: %s", item, exc)

        if self.work_dir.exists():
            # 工作文件（推理中间产物）至少保留 1 小时，避免误删正在生成的文件，
            # 同时保证异常中断后的残留文件最终也会被清掉。
            work_expire = max(self.expire_seconds, 3600.0)
            for item in self.work_dir.iterdir():
                if not item.is_file():
                    continue
                try:
                    stat = item.stat()
                except OSError:
                    continue
                if now - stat.st_mtime > work_expire:
                    try:
                        item.unlink()
                        removed_work += 1
                        freed_bytes += stat.st_size
                    except OSError as exc:
                        logger.warning("清理临时文件 %s 失败: %s", item, exc)

        return {
            "removed_audio": removed_audio,
            "removed_work": removed_work,
            "freed_bytes": freed_bytes,
        }

    # ---------------- 后台任务 ----------------

    async def start_cleanup_loop(self, interval_minutes: float) -> None:
        interval = max(60.0, float(interval_minutes) * 60.0)
        self.ensure_dirs()

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.sleep(interval)
                    result = await asyncio.to_thread(self.cleanup)
                    if result["removed_audio"] or result["removed_work"]:
                        logger.info(
                            "清理完成: 音频 %d 个, 临时文件 %d 个, 释放 %.1f KB",
                            result["removed_audio"],
                            result["removed_work"],
                            result["freed_bytes"] / 1024,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - 清理失败不能影响主流程
                    logger.exception("后台清理任务异常")

        self._cleanup_task = asyncio.create_task(_loop(), name="tts-audio-cleanup")

    async def stop_cleanup_loop(self) -> None:
        if self._cleanup_task is None:
            return
        self._cleanup_task.cancel()
        try:
            await self._cleanup_task
        except asyncio.CancelledError:
            pass
        finally:
            self._cleanup_task = None

    def stats(self) -> dict[str, object]:
        count = 0
        total_bytes = 0
        if self.output_dir.exists():
            for item in self.output_dir.iterdir():
                if item.is_file() and FILENAME_RE.match(item.name):
                    count += 1
                    try:
                        total_bytes += item.stat().st_size
                    except OSError:
                        pass
        return {
            "files": count,
            "total_mb": round(total_bytes / 1024 / 1024, 3),
            "expire_minutes": pretty_number(self.expire_minutes),
            "expire_seconds": round(self.expire_seconds, 1),
        }
