"""管理 / 健康检查 endpoints."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from sqlalchemy import text

from app import beets_bridge
from app.config import get_settings
from app.db import get_engine
from app.workers import queue

log = logging.getLogger(__name__)

router = APIRouter()

_running_scans: set[asyncio.Task] = set()


@router.get("/stats")
async def stats() -> dict:
    """库整体状况一览."""
    s = get_settings()
    out: dict = {
        "music_root": str(s.music_root),
        "data_dir": str(s.data_dir),
        "items_total": 0,
        "items_identified": 0,
        "queue": queue.counts_by_status(),
    }
    if s.beets_db.exists():
        try:
            lib = beets_bridge.get_library(s.beets_db, s.music_root)
            total = beets_bridge.count_items(lib)
            rec = beets_bridge.count_at_recording_level(lib)
            rg = beets_bridge.count_at_releasegroup_level(lib)
            artist_only = beets_bridge.count_at_artist_only(lib)
            identified = beets_bridge.count_identified(lib)
            out["items_total"] = total
            out["items_identified"] = identified
            out["items_unidentified"] = total - identified
            out["by_level"] = {
                "recording (fingerprint)": rec,
                "release_group (album)": rg - rec if rg > rec else rg,
                "artist_only": artist_only,
            }
        except Exception as e:
            log.exception("stats: beets read failed")
            out["beets_error"] = str(e)

    # MB cache 状况
    try:
        with get_engine().connect() as conn:
            row = conn.execute(text("SELECT COUNT(*) FROM mb_artist")).first()
            out["mb_cache"] = {
                "artists": int(row[0]) if row else 0,
                "release_groups": int(
                    conn.execute(text("SELECT COUNT(*) FROM mb_release_group")).first()[0]
                ),
            }
    except Exception:
        pass
    return out


@router.post("/scan")
async def trigger_scan() -> dict:
    """触发一次增量扫描（非阻塞；返回 task 状态）.

    一次只允许跑一个扫描；正在跑就拒绝.
    """
    from app.workers.scan import scan_and_import  # noqa: PLC0415

    if any(not t.done() for t in _running_scans):
        return {"ok": False, "reason": "scan already running"}

    task = asyncio.create_task(scan_and_import(), name="scan")
    _running_scans.add(task)
    task.add_done_callback(_running_scans.discard)
    return {"ok": True, "note": "scan started in background; watch /api/v1/admin/stats"}


@router.get("/queue")
async def queue_status() -> dict:
    """队列里各 kind × status 的计数."""
    return {"rows": queue.counts_by_kind_status()}


@router.get("/queue/recent")
async def queue_recent(limit: int = 20) -> dict:
    """最近 N 条任务（debug 用）."""
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """SELECT id, kind, status, attempts, last_error,
                          created_at, started_at, finished_at
                   FROM task_queue
                   ORDER BY id DESC
                   LIMIT :n"""
            ),
            {"n": limit},
        ).all()
        return {"tasks": [dict(r._mapping) for r in rows]}


@router.get("/diagnose-archives")
async def diagnose_archives(sample: int = 5) -> dict:
    """一次性诊断 rar / zip / 7z 不被处理的常见原因。

    回答几个排查问题：
    - .env 里 ALLOW_FILE_WRITES 开了吗？(关 = worker 直接跳过)
    - unar 装了吗？(没装 = 解不了 RAR/7z)
    - music_root 下到底有几个待解压档？已经解过几个？
    - task_queue 里 archive_extract 任务什么状态？最近报错？

    iOS 端看不到这些状态，直接 GET 这个 endpoint 就是一键体检。
    """
    from app import archive  # noqa: PLC0415

    s = get_settings()

    # 1) ALLOW_FILE_WRITES
    writes_ok = s.allow_file_writes

    # 2) unar 在不在
    unar_ok = archive.unar_available()

    # 3) music_root 扫一遍，分类 pending / extracted
    archives_found = archive.detect_archives(s.music_root)
    pending: list[str] = []
    extracted: list[str] = []
    for arc in archives_found:
        rel = str(arc.relative_to(s.music_root)) if arc.is_relative_to(s.music_root) \
            else str(arc)
        if archive.is_already_extracted(arc):
            extracted.append(rel)
        else:
            pending.append(rel)

    # 4) task_queue 里 archive_extract 最近状态
    queue_rows: list[dict] = []
    queue_counts: dict[str, int] = {}
    with get_engine().connect() as conn:
        for r in conn.execute(
            text(
                """SELECT status, COUNT(*) AS n
                   FROM task_queue
                   WHERE kind = 'archive_extract'
                   GROUP BY status"""
            )
        ).all():
            queue_counts[r[0]] = int(r[1])

        for r in conn.execute(
            text(
                """SELECT id, status, attempts, last_error,
                          payload, created_at, started_at, finished_at
                   FROM task_queue
                   WHERE kind = 'archive_extract'
                   ORDER BY id DESC
                   LIMIT :n"""
            ),
            {"n": max(1, sample)},
        ).all():
            queue_rows.append(dict(r._mapping))

    # 5) 综合 verdict —— 给个一句话的"为什么没处理"
    verdict: str
    if not writes_ok:
        verdict = "ALLOW_FILE_WRITES=false → worker 跳过所有解压。改 .env 后重启。"
    elif not unar_ok and pending:
        verdict = "unar 没装 → 没法解 RAR/7z。apt/brew install unar 后 POST /scan。"
    elif not pending and not extracted:
        verdict = "music_root 里没找到任何 .rar/.zip/.7z。文件放对地方了吗？"
    elif not pending and extracted:
        verdict = f"OK — 全部 {len(extracted)} 个档已解过了。"
    elif pending and not queue_rows:
        verdict = f"待解 {len(pending)} 个，但 task_queue 里没排队。POST /api/v1/admin/scan 触发。"
    elif pending and queue_counts.get("failed", 0):
        verdict = f"待解 {len(pending)} 个，{queue_counts.get('failed', 0)} 个失败。看下面 sample 里的 last_error。"
    else:
        verdict = f"队列正在跑：{queue_counts}。耐心等。"

    return {
        "verdict": verdict,
        "allow_file_writes": writes_ok,
        "unar_available": unar_ok,
        "music_root": str(s.music_root),
        "pending": {
            "count": len(pending),
            "sample": pending[:sample],
        },
        "extracted": {
            "count": len(extracted),
            "sample": extracted[:sample],
        },
        "queue": {
            "by_status": queue_counts,
            "recent": queue_rows,
        },
    }


@router.post("/identify-unidentified")
async def identify_unidentified() -> dict:
    """给所有还没识别（无 mb_trackid）的 item 排 fingerprint 任务."""
    s = get_settings()
    if not s.beets_db.exists():
        return {"ok": False, "reason": "beets DB 不存在，先 POST /scan"}
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    ids = list(beets_bridge.iter_unidentified(lib))
    enqueued = queue.enqueue_many("fingerprint", [{"item_id": i} for i in ids])
    return {"ok": True, "enqueued": enqueued, "has_acoustid_key": bool(s.acoustid_api_key)}


@router.get("/cue-flac-pairs")
async def list_cue_flac_pairs() -> dict:
    """预览所有 CUE+源音频对（不动文件）."""
    from app import cuesplit as _cs  # noqa: PLC0415
    s = get_settings()
    pairs = _cs.detect_pairs(s.music_root)
    out = []
    for cue, src in pairs:
        try:
            sheet = _cs.parse_cue(cue)
            out.append({
                "cue": str(cue),
                "src_audio": str(src),
                "tracks": len(sheet.tracks),
                "album": sheet.title,
                "performer": sheet.performer,
            })
        except Exception as e:
            out.append({"cue": str(cue), "src_audio": str(src), "error": str(e)})
    return {"count": len(out), "pairs": out}


@router.post("/scan-cue-flac")
async def scan_cue_flac() -> dict:
    """全库重新扫 CUE+音频对，全部 enqueue cue_split（用于已经扫库过的现有数据）."""
    from app import cuesplit as _cs  # noqa: PLC0415
    s = get_settings()
    pairs = _cs.detect_pairs(s.music_root)
    enqueued = queue.enqueue_many(
        "cue_split",
        [{"cue": str(c), "src_audio": str(a)} for c, a in pairs],
    )
    return {"ok": True, "enqueued": enqueued, "allow_file_writes": s.allow_file_writes}


@router.post("/dedupe-paths")
async def dedupe_paths() -> dict:
    """合并因 beets 路径不规范化造成的重复 item 行."""
    s = get_settings()
    if not s.beets_db.exists():
        return {"ok": False, "reason": "no beets DB"}
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    removed = beets_bridge.dedupe_items_by_path(lib)
    return {"ok": True, "removed": removed}


@router.post("/clear-mb-ids")
async def clear_mb_ids(confirm: bool = False) -> dict:
    """清掉所有 item 上的 mb_* 字段 + MB 缓存表，让识别从头来.

    误识别后救命用。需 ?confirm=true.
    """
    if not confirm:
        return {"ok": False, "reason": "需要 ?confirm=true"}
    s = get_settings()
    if not s.beets_db.exists():
        return {"ok": False, "reason": "no beets DB"}

    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    cleared = 0
    for it in lib.items():
        if it.mb_trackid or it.mb_releasegroupid or it.mb_albumid:
            it.mb_trackid = ""
            it.mb_releasegroupid = ""
            it.mb_artistid = ""
            it.mb_albumartistid = ""
            it.mb_albumid = ""
            it.store()
            cleared += 1
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM mb_release_group"))
        conn.execute(text("DELETE FROM mb_artist"))
    return {"ok": True, "cleared_items": cleared}


@router.post("/backfill-album-artist")
async def backfill_album_artist(dry_run: bool = True) -> dict:
    """把所有 item.albumartist 用 mb_artist 缓存里的 canonical 名修正。

    场景：曾经的 identify / fingerprint 写回只更了 mb_albumartistid，没动
    it.albumartist。结果 organize 算 dst 的时候用旧 tag 里的名字，目录名
    永远不会被规范化（例如 那英 / 张惠妹 这种被旧 tag 占着的）。

    dry_run=true（默认）只报告会改哪些；?dry_run=false 才真正写。
    要让目录跟着改：跑完这个之后再去 organize 页面 Apply。
    """
    s = get_settings()
    if not s.beets_db.exists():
        return {"ok": False, "reason": "beets DB 不存在"}
    lib = beets_bridge.get_library(s.beets_db, s.music_root)

    # 一次性把 mb_artist 表读进 dict，避免 N 次查询
    with get_engine().connect() as conn:
        name_by_mbid = {
            r[0]: r[1]
            for r in conn.execute(text("SELECT mbid, name FROM mb_artist")).all()
            if r[0] and r[1]
        }

    diffs: list[dict] = []
    updated = 0
    for it in lib.items():
        mbid = getattr(it, "mb_albumartistid", "") or getattr(it, "mb_artistid", "")
        if not mbid:
            continue
        canonical = name_by_mbid.get(mbid)
        if not canonical:
            continue
        old = it.albumartist or ""
        if old == canonical:
            continue
        diffs.append({
            "item_id": int(it.id),
            "from": old,
            "to": canonical,
            "mbid": mbid,
        })
        if not dry_run:
            it.albumartist = canonical
            it.store()
            updated += 1

    return {
        "ok": True,
        "dry_run": dry_run,
        "candidates": len(diffs),
        "updated": updated,
        "sample": diffs[:30],
    }


@router.post("/refresh-artists")
async def refresh_artists() -> dict:
    """给已识别 item 涉及的所有 artist 排一次 mb_fetch_artist."""
    s = get_settings()
    if not s.beets_db.exists():
        return {"ok": False, "reason": "beets DB 不存在"}
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    artist_mbids = beets_bridge.iter_unique_albumartist_mbids(lib)
    enqueued = queue.enqueue_many(
        "mb_fetch_artist",
        [{"artist_mbid": mbid} for mbid in artist_mbids],
    )
    return {"ok": True, "enqueued": enqueued, "artists": len(artist_mbids)}
