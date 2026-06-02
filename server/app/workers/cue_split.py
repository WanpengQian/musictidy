"""Worker: CUE+FLAC 切轨任务.

入队来源（两条）：
  1. scan worker：扫描时发现 CUE+FLAC 对就 enqueue
  2. admin endpoint：用户手动触发一次全库扫描

任务流：
  payload = {"cue": str, "src_audio": str}

  1. ALLOW_FILE_WRITES 检查（关了直接静默完成，不重试；用户开了再触发）
  2. ffmpeg 切轨 → 同目录下生成 NN. Title.flac
  3. 原 CUE + 原源音频 → mv 到 .trash/cuesplit_<ts>/
  4. beets DB：删原 item（path 已失效）
  5. beets DB：import 新文件 + enqueue fingerprint
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from app import beets_bridge, cuesplit
from app.config import get_settings
from app.workers import queue

log = logging.getLogger(__name__)

_warned_no_writes = False


def _resolve_item_path(item, music_root: Path) -> Path:
    p = Path(os.fsdecode(item.path))
    if not p.is_absolute():
        p = music_root / p
    return p.resolve()


async def handle_cue_split(payload: dict[str, Any]) -> None:
    global _warned_no_writes
    s = get_settings()

    cue_path = Path(payload["cue"])
    src_audio = Path(payload["src_audio"])

    if not s.allow_file_writes:
        if not _warned_no_writes:
            log.warning(
                "cue_split: ALLOW_FILE_WRITES=false；切轨任务全跳过。"
                "开了之后 POST /api/v1/admin/scan-cue-flac 重新入队即可。"
            )
            _warned_no_writes = True
        return

    if not cue_path.exists() or not src_audio.exists():
        log.info("cue_split: 文件已不在: %s / %s", cue_path.name, src_audio.name)
        return

    # 切到临时目录（避免和源文件名碰撞 → "(split)" 后缀垃圾）
    final_dst_dir = src_audio.parent
    with tempfile.TemporaryDirectory(dir=str(s.trash_dir)) as tmp:
        try:
            tmp_paths = await asyncio.to_thread(
                cuesplit.split_pair, cue_path, src_audio, Path(tmp)
            )
        except Exception:
            log.exception("cue_split: 切轨失败 %s", src_audio)
            raise  # 让 queue 重试

        # 切成功 → 把原 CUE+源音频 mv 到 trash
        trash_dir = s.trash_dir / f"cuesplit_{int(time.time())}_{src_audio.stem[:30]}"
        trash_dir.mkdir(parents=True, exist_ok=True)
        for orig in (src_audio, cue_path):
            try:
                shutil.move(str(orig), str(trash_dir / orig.name))
            except Exception as e:
                log.warning("cue_split: trash move 失败 %s: %s", orig, e)

        # 把临时目录里的 split 文件 mv 到原目录（现在无冲突了）
        new_paths: list[Path] = []
        for tp in tmp_paths:
            final = final_dst_dir / tp.name
            try:
                shutil.move(str(tp), str(final))
                new_paths.append(final)
            except Exception as e:
                log.error("cue_split: 移到目标位置失败 %s: %s", tp, e)

    log.info("cue_split: %s → %d 个新 FLAC", src_audio.name, len(new_paths))

    # beets DB：删旧 item + 加新 item + enqueue fingerprint
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    music_root = Path(
        lib.directory.decode() if isinstance(lib.directory, (bytes, memoryview))
        else lib.directory
    )

    target = src_audio.resolve()
    for it in list(lib.items()):
        try:
            if _resolve_item_path(it, music_root) == target:
                it.remove()
                log.info("cue_split: 已从 beets DB 移除原 item id=%d", it.id)
                break
        except Exception:
            continue

    new_ids: list[int] = []
    for p in new_paths:
        try:
            iid = beets_bridge.import_file(lib, p)
            if iid is not None:
                new_ids.append(iid)
        except Exception as e:
            log.warning("cue_split: import 新文件失败 %s: %s", p, e)

    if new_ids:
        queue.enqueue_many("fingerprint", [{"item_id": i} for i in new_ids])
        log.info("cue_split: %d 个新 item 已排 fingerprint", len(new_ids))


# ── Test helpers ───────────────────────────────────────────────
def _reset_for_tests() -> None:
    global _warned_no_writes
    _warned_no_writes = False
