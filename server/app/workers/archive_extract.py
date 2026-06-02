"""Worker: zip/rar/7z 自动解压.

任务流：
  payload = {"archive": str}

  1. ALLOW_FILE_WRITES 闸 → 关了直接跳过（标 done，不重试）
  2. 已解过（_extracted/<name>/ 非空）→ 不重解，直接把源档移 trash
  3. 没解过 → unar 解 → 源档进 trash
  4. 下一次 scan 自动抓里头新音频
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from app import archive
from app.config import get_settings
from app.workers import queue

log = logging.getLogger(__name__)

_warned_no_writes = False
_warned_no_unar = False


def _move_to_trash(arc: Path, trash_root: Path) -> None:
    trash_dir = trash_root / f"archive_{int(time.time())}_{arc.stem[:30]}"
    trash_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(arc), str(trash_dir / arc.name))
    except Exception as e:
        log.warning("archive: trash 移动失败 %s: %s", arc, e)


async def handle_archive_extract(payload: dict[str, Any]) -> None:
    global _warned_no_writes, _warned_no_unar
    s = get_settings()

    arc = Path(payload["archive"])
    if not arc.exists():
        log.info("archive: 文件已不在 %s", arc.name)
        return

    if not s.allow_file_writes:
        if not _warned_no_writes:
            log.warning(
                "archive: ALLOW_FILE_WRITES=false; 跳过所有解压任务。"
                "开了之后 POST /api/v1/admin/scan 重新触发。"
            )
            _warned_no_writes = True
        return

    # 已解过 → 不重解，把源档收到 trash
    if archive.is_already_extracted(arc):
        log.info("archive: %s 已存在解压结果，移源档到 trash", arc.name)
        _move_to_trash(arc, s.trash_dir)
        return

    # 没解 + unar 不可用 → 致命，重试
    if not archive.unar_available():
        if not _warned_no_unar:
            log.error("archive: unar 不可用; brew install unar / pkg install unar")
            _warned_no_unar = True
        raise RuntimeError("unar not found")

    # 头部 magic 预检：源档不是有效 RAR/ZIP/7z → 抛 PermanentTaskError，
    # scheduler 看到立刻 failed，跳过 5 次 unar 重试（每次都要起 subprocess）
    bad = archive.validate_magic(arc)
    if bad is not None:
        log.warning("archive: %s 拒绝解压 — %s", arc.name, bad)
        raise queue.PermanentTaskError(f"{arc.name}: {bad}")

    # 解压（subprocess 阻塞 → to_thread）
    try:
        dst = await asyncio.to_thread(archive.extract, arc)
    except Exception:
        log.exception("archive: 解压失败 %s", arc.name)
        raise  # 让队列重试

    try:
        rel = dst.relative_to(arc.parent)
    except ValueError:
        rel = dst
    log.info("archive: %s → %s", arc.name, rel)

    _move_to_trash(arc, s.trash_dir)


def _reset_for_tests() -> None:
    global _warned_no_writes, _warned_no_unar
    _warned_no_writes = False
    _warned_no_unar = False
