"""按需转码 —— 全内存。

ffmpeg pipe stdout → bytes → 进程内 LRU 字典。HTTP Range 直接在 bytes 上切片。
重启即清；SSD 零写入。
"""

from __future__ import annotations

import asyncio
import collections
import logging
import shutil
from pathlib import Path
from typing import Tuple

log = logging.getLogger(__name__)


# ─── 内存缓存（LRU） ─────────────────────────────────────────────────────

class MemCache:
    """简单字节 LRU；按总字节数淘汰。"""

    def __init__(self, max_bytes: int = 500 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes
        self.cache: collections.OrderedDict[Tuple, Tuple[bytes, str]] = collections.OrderedDict()
        self.total: int = 0

    def get(self, key: Tuple) -> Tuple[bytes, str] | None:
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None

    def put(self, key: Tuple, data: bytes, mime: str) -> None:
        if key in self.cache:
            old_data, _ = self.cache[key]
            self.total -= len(old_data)
        self.cache[key] = (data, mime)
        self.cache.move_to_end(key)
        self.total += len(data)
        while self.total > self.max_bytes and self.cache:
            ev_key, (ev_data, _) = self.cache.popitem(last=False)
            self.total -= len(ev_data)
            log.info("evicted %s (%d MB), cache=%d MB",
                     ev_key, len(ev_data) >> 20, self.total >> 20)


_cache = MemCache(max_bytes=500 * 1024 * 1024)

# 同 key 的并发转码请求只跑一次 ffmpeg
_locks: dict[Tuple, asyncio.Lock] = {}

# ffmpeg 进程并发上限
_sem: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(2)
    return _sem


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# ─── 转码 ────────────────────────────────────────────────────────────────

async def _run_ffmpeg(args: list[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode(errors="replace")[-1500:]
        raise RuntimeError(f"ffmpeg exit {proc.returncode}: {msg}")
    return stdout


async def get_or_transcode_aac(item_id: int, src: Path, bitrate: int) -> Tuple[bytes, str]:
    if not _ffmpeg_available():
        raise RuntimeError("ffmpeg not installed")

    key = (item_id, "aac", bitrate)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
        async with _get_sem():
            log.info("transcoding item=%d → AAC %dk (mem)", item_id, bitrate)
            data = await _run_ffmpeg([
                "ffmpeg", "-loglevel", "warning",
                "-i", str(src),
                "-vn",
                "-c:a", "aac",
                "-b:a", f"{bitrate}k",
                "-f", "adts",            # ADTS 流式 AAC，pipe 输出 OK
                "pipe:1",
            ])
            _cache.put(key, data, "audio/aac")
    return _cache.get(key)  # type: ignore[return-value]


async def get_or_transcode_flac(item_id: int, src: Path) -> Tuple[bytes, str]:
    if not _ffmpeg_available():
        raise RuntimeError("ffmpeg not installed")

    key = (item_id, "flac", 0)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
        async with _get_sem():
            log.info("transcoding item=%d → FLAC (mem)", item_id)
            data = await _run_ffmpeg([
                "ffmpeg", "-loglevel", "warning",
                "-i", str(src),
                "-vn",
                "-c:a", "flac",
                "-compression_level", "3",
                "-f", "flac",
                "pipe:1",
            ])
            _cache.put(key, data, "audio/flac")
    return _cache.get(key)  # type: ignore[return-value]


def cache_stats() -> dict:
    return {
        "entries": len(_cache.cache),
        "bytes": _cache.total,
        "mb": _cache.total >> 20,
        "max_mb": _cache.max_bytes >> 20,
    }
