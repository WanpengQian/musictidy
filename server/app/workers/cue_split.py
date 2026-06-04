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
    # 上层 (detector / scan) 知道这是哪张 MB rg 就传过来, 用来给新 item 钉
    # rg + recording_mbid (按 position). 没传就保持现有行为 (跑 fingerprint).
    rg_mbid = (payload.get("rg_mbid") or "").strip()

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

    # 切前先记 source 文件指纹 (size+mtime), 切完删之前再校验.
    # 防止: 切轨期间用户重新拷了一份同名文件 → 老逻辑 unlink 会把新文件删掉。
    try:
        src_sig0 = (src_audio.stat().st_size, src_audio.stat().st_mtime)
        cue_sig0 = (cue_path.stat().st_size, cue_path.stat().st_mtime)
    except OSError as e:
        log.warning("cue_split: stat 源失败 %s: %s", src_audio, e)
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

        # 切成功 → 直接删原 CUE + 源音频
        # ffmpeg lossless → lossless 重编码 bit-perfect, splits 已是用户最终
        # 想要的文件, 没必要留 500MB 的整轨源在 trash 里慢慢堆。
        # 用户如果真要 undo, 从他自己的 NAS / 备份源拿就行。
        for orig, sig0 in ((src_audio, src_sig0), (cue_path, cue_sig0)):
            try:
                st = orig.stat()
                if (st.st_size, st.st_mtime) != sig0:
                    log.warning(
                        "cue_split: 源 %s 期间被改/重拷 (size/mtime 变), 不删, 保留新文件",
                        orig.name,
                    )
                    continue
                orig.unlink()
            except OSError as e:
                log.warning("cue_split: 删源 %s 失败: %s", orig, e)

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

    # scan 直接 enqueue cue_split 时 payload 没有 rg_mbid; 源也不会被
    # fingerprint (scan 故意跳过整轨源, line 94). 所以靠"从源 item 继承"
    # 大概率失败 → 加 .musictidy.json sidecar 兜底, 用户手填 rg_mbid 就钉死。
    target = src_audio.resolve()
    for it in list(lib.items()):
        try:
            if _resolve_item_path(it, music_root) == target:
                if not rg_mbid and it.mb_releasegroupid:
                    rg_mbid = it.mb_releasegroupid
                    log.info(
                        "cue_split: 从源 item id=%d 继承 rg=%s",
                        it.id, rg_mbid,
                    )
                it.remove()
                log.info("cue_split: 已从 beets DB 移除原 item id=%d", it.id)
                break
        except Exception:
            continue

    if not rg_mbid:
        from app import info_sidecar  # noqa: PLC0415
        sc = info_sidecar.read(src_audio.parent)
        if sc and (sc.get("rg_mbid") or "").strip():
            rg_mbid = sc["rg_mbid"].strip()
            log.info("cue_split: 从 .musictidy.json sidecar 拿 rg=%s", rg_mbid)

    # 若上层传了 rg_mbid 且 MB tracks_json 数量跟切出来的曲数一致, 按 position
    # 给每个新 item 钉死 rg + recording_mbid (跟 split_by_album 一样, 避免被
    # 后续 fingerprint 把里头某首识到别张专辑去, 同时让中文小众专辑也能直接绑)
    mb_tracks: list[dict] = []
    if rg_mbid:
        try:
            from app.db import get_engine  # noqa: PLC0415
            from sqlalchemy import text  # noqa: PLC0415
            with get_engine().connect() as conn:
                row = conn.execute(
                    text("SELECT tracks_json FROM mb_release_group WHERE mbid=:m"),
                    {"m": rg_mbid},
                ).first()
            import json as _json  # noqa: PLC0415
            if row and row.tracks_json:
                raw = _json.loads(row.tracks_json)
                if isinstance(raw, list) and len(raw) == len(new_paths):
                    mb_tracks = sorted(raw, key=lambda t: int(t.get("position") or 0))
        except Exception:  # noqa: BLE001
            log.exception("cue_split: 读 MB tracks_json 失败 rg=%s", rg_mbid)

    new_ids: list[int] = []
    for i, p in enumerate(new_paths):
        try:
            iid = beets_bridge.import_file(lib, p)
            if iid is None:
                continue
            new_ids.append(iid)
            if mb_tracks:
                rec_mbid = mb_tracks[i].get("recording_mbid") or None
                beets_bridge.set_mb_ids(
                    lib, iid,
                    track_mbid=rec_mbid,
                    releasegroup_mbid=rg_mbid,
                    artist_mbid=None,
                    album_artist_mbid=None,
                    album_artist=None,
                )
        except Exception as e:
            log.warning("cue_split: import 新文件失败 %s: %s", p, e)

    if new_ids:
        queue.enqueue_many("fingerprint", [{"item_id": i} for i in new_ids])
        log.info(
            "cue_split: %d 个新 item 已排 fingerprint (rg pre-bind: %s)",
            len(new_ids), bool(mb_tracks),
        )


# ── Test helpers ───────────────────────────────────────────────
def _reset_for_tests() -> None:
    global _warned_no_writes
    _warned_no_writes = False
