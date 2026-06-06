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


_running_backfills: set[asyncio.Task] = set()


@router.post("/backfill-release-groups")
async def backfill_release_groups() -> dict:
    """补 mb_release_group 缓存里缺的行。

    症状: item 有 mb_releasegroupid (beets 导入时带的 MB tag), 但 server 的
    mb_release_group 缓存表从没为它填过行 → owned-albums 起手 FROM 这张表,
    命不中 → 整张专辑在 Browse 里隐身 (典型: 张学友 12 个 item 全在却一张
    专辑不显示)。

    扫所有非空 releasegroupid, 把缺的逐个从 MB 拉回补上。MB 限速 1 req/s,
    所以慢、跑后台; 缺多少先返回, 跑完看 /api/v1/admin/stats 的 mb_cache。
    """
    from app.workers.sync_sidecars import (  # noqa: PLC0415
        backfill_missing_release_groups,
    )

    if any(not t.done() for t in _running_backfills):
        return {"ok": False, "reason": "backfill already running"}

    with get_engine().connect() as conn:
        missing = int(conn.execute(text(
            """SELECT COUNT(DISTINCT mb_releasegroupid)
               FROM beets.items
               WHERE mb_releasegroupid IS NOT NULL
                 AND mb_releasegroupid != ''
                 AND mb_releasegroupid NOT IN
                     (SELECT mbid FROM mb_release_group)"""
        )).scalar() or 0)

    if missing == 0:
        return {"ok": True, "missing": 0, "note": "缓存已齐, 没活可干"}

    # 同步 + sleep 的活, 丢线程跑, 别堵 event loop
    task = asyncio.create_task(
        asyncio.to_thread(backfill_missing_release_groups), name="backfill-rg",
    )
    _running_backfills.add(task)
    task.add_done_callback(_running_backfills.discard)
    return {
        "ok": True,
        "missing": missing,
        "note": f"补 {missing} 个 rg, 后台跑 (约 {missing} 秒, MB 限速 1/s); "
                "完事看 /api/v1/admin/stats",
    }


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


@router.post("/scan-split-suggestions")
async def scan_split_suggestions() -> dict:
    """全库扫一遍「单文件 = 整张专辑」候选, 给每条 hit 入 split_suggestion。

    场景: 用户库里早就 fingerprint 过的大 FLAC, 当时 mb_release_group.tracks_json
    可能还没缓存所以漏检; 用户在 web 点这个按钮一次性补齐建议列表。
    """
    from sqlalchemy import text  # noqa: PLC0415

    from app.api.library import (  # noqa: PLC0415
        _SPLIT_LOSSLESS_FORMATS,
        _SPLIT_MIN_DURATION_S,
        maybe_enqueue_split_suggestion,
    )
    from app.db import get_engine  # noqa: PLC0415

    fmts_csv = ",".join(f"'{f}'" for f in _SPLIT_LOSSLESS_FORMATS)
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                f"""SELECT id, mb_releasegroupid
                    FROM beets.items
                    WHERE length > :min_len
                      AND UPPER(COALESCE(format,'')) IN ({fmts_csv})
                      AND COALESCE(mb_releasegroupid,'') != ''"""
            ),
            {"min_len": _SPLIT_MIN_DURATION_S},
        ).all()
    inserted = 0
    for r in rows:
        if maybe_enqueue_split_suggestion(int(r.id), r.mb_releasegroupid):
            inserted += 1
    return {"ok": True, "scanned": len(rows), "new_suggestions": inserted}


@router.post("/sync-sidecars")
async def sync_sidecars() -> dict:
    """把当前 beets 的识别结果同步成 .musictidy.json 写到各专辑目录里。

    每个目录里所有 items 都绑到同一个 rg → 写 sidecar 锁定. 不同 rg 混着
    的目录跳过 (说明这张专辑还没整理干净, 不能定结论)。

    用法:
    - scan 完, 你确认识别结果差不多 OK 了, 调一下这个 endpoint, 把成果
      落到 disk 上.
    - 下次重新拷贝文件 / wipe + scan, fingerprint worker 起手读 sidecar
      就能自动按 rg + position 钉死, 不用再人工纠正一遍 best-of 污染。
    """
    import os as _os  # noqa: PLC0415
    from collections import defaultdict  # noqa: PLC0415
    from pathlib import Path as PPath  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    from app import info_sidecar  # noqa: PLC0415
    from app.db import get_engine  # noqa: PLC0415

    def _decode(p) -> str:
        if isinstance(p, (bytes, memoryview)):
            return bytes(p).decode("utf-8", errors="replace")
        return p or ""

    # beets 存的可能是相对路径; 补绝对
    music_root = get_settings().music_root

    def _abs(p_str: str) -> str:
        if not p_str:
            return ""
        pp = PPath(p_str)
        if not pp.is_absolute():
            pp = music_root / pp
        return str(pp)

    # 跑一遍 dominant-per-folder 合并 (同 dir items candidate_rgs 投票)
    try:
        from app.workers.sync_sidecars import _consolidate_by_folder  # noqa: PLC0415
        _consolidate_by_folder()
    except Exception as e:  # noqa: BLE001
        log.warning("admin/sync-sidecars: consolidation 失败 %s", e)

    by_dir: dict[str, dict] = defaultdict(lambda: {
        "rgs": set(), "artist_mbids": set(),
        "album": "", "artist": "",
    })
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """SELECT mb_releasegroupid, mb_artistid, mb_albumartistid,
                          path, album, artist
                   FROM beets.items
                   WHERE mb_releasegroupid IS NOT NULL
                     AND mb_releasegroupid != ''"""
            )
        ).all()
    for r in rows:
        p = _abs(_decode(r.path))
        if not p:
            continue
        d = _os.path.dirname(p)
        slot = by_dir[d]
        slot["rgs"].add(r.mb_releasegroupid)
        am = (r.mb_albumartistid or r.mb_artistid or "").strip()
        if am:
            slot["artist_mbids"].add(am)
        if not slot["album"] and r.album:
            slot["album"] = r.album
        if not slot["artist"] and r.artist:
            slot["artist"] = r.artist

    written, skipped_mixed, skipped_nodir = 0, 0, 0
    for d, slot in by_dir.items():
        if len(slot["rgs"]) != 1:
            skipped_mixed += 1
            continue
        dpath = PPath(d)
        if not dpath.is_dir():
            skipped_nodir += 1
            continue
        rg = next(iter(slot["rgs"]))
        fields: dict = {"rg_mbid": rg}
        if len(slot["artist_mbids"]) == 1:
            fields["artist_mbid"] = next(iter(slot["artist_mbids"]))
        if slot["album"]:
            fields["_album"] = slot["album"]
        if slot["artist"]:
            fields["_artist"] = slot["artist"]
        if info_sidecar.write(dpath, fields):
            written += 1
    return {
        "ok": True,
        "dirs_total": len(by_dir),
        "sidecars_written": written,
        "skipped_mixed_rg": skipped_mixed,
        "skipped_no_dir": skipped_nodir,
    }


@router.post("/scheduler/pause-scan")
async def pause_scheduled_scan() -> dict:
    """临时暂停 30 分钟自动扫描 (测试场景: 用户在一个一个加目录).
    server 重启自动恢复。
    """
    from app.workers import scheduler  # noqa: PLC0415
    try:
        scheduler._sched.remove_job("scan")  # type: ignore[attr-defined]
        return {"ok": True, "paused": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": str(e)}


@router.post("/scheduler/resume-scan")
async def resume_scheduled_scan() -> dict:
    """重新启用自动扫描 (跟启动后的默认行为一样, 30 min 一次)."""
    from datetime import datetime, timedelta  # noqa: PLC0415

    from app.workers import scheduler  # noqa: PLC0415
    from app.workers.scan import scan_and_import  # noqa: PLC0415

    try:
        scheduler._sched.add_job(  # type: ignore[attr-defined]
            scheduler._wrap_async(scan_and_import),  # type: ignore[attr-defined]
            trigger="interval",
            minutes=30,
            id="scan",
            next_run_time=datetime.now() + timedelta(seconds=30),
            replace_existing=True,
        )
        return {"ok": True, "resumed": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": str(e)}


@router.post("/cleanup-stale")
async def cleanup_stale() -> dict:
    """删除 beets 里指向已不存在文件的 items.

    场景: 用户在 MusicTidy 不知情时直接删了音乐文件 / 改了路径,
    items 留下"幽灵记录" → 库统计虚高, 播放 ENOENT。

    路径不存在 → 直接 remove. 不动文件 (它本来就没了).
    """
    s = get_settings()
    if not s.beets_db.exists():
        return {"ok": False, "reason": "no beets DB"}
    lib = beets_bridge.get_library(s.beets_db, s.music_root)

    removed = 0
    scanned = 0
    for it in list(lib.items()):
        scanned += 1
        try:
            from app.workers.scan import Path as _Path  # noqa: PLC0415
            import os as _os  # noqa: PLC0415
            raw = it.path
            if isinstance(raw, (bytes, memoryview)):
                p = _Path(bytes(raw).decode("utf-8", errors="replace"))
            else:
                p = _Path(str(raw))
            if not p.is_absolute():
                music_root = _Path(
                    lib.directory.decode() if isinstance(lib.directory, (bytes, memoryview))
                    else lib.directory
                )
                p = music_root / p
            if not _os.path.exists(p):
                it.remove()
                removed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("cleanup-stale: 处理 item %s 出错: %s", getattr(it, "id", "?"), e)
            continue
    return {"ok": True, "scanned": scanned, "removed_stale": removed}


@router.post("/dedupe-paths")
async def dedupe_paths() -> dict:
    """合并因 beets 路径不规范化造成的重复 item 行."""
    s = get_settings()
    if not s.beets_db.exists():
        return {"ok": False, "reason": "no beets DB"}
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    removed = beets_bridge.dedupe_items_by_path(lib)
    return {"ok": True, "removed": removed}


@router.post("/wipe-library")
async def wipe_library(confirm: bool = False) -> dict:
    """把库整个清空, 让用户重新拷文件 + 重新扫描跑全流程.

    清:
      - beets DB: items / albums (+ attribute 子表)
      - musictidy DB: split_suggestion / track_fingerprint / task_queue /
        item_decision / pending_decision / playlist_item / transcode_cache /
        trash_log

    保留:
      - beets migrations
      - musictidy: auth_session (别踢登录) / mb_artist + mb_release_group
        (MB 缓存, 包含 tracks_json, 不要白白丢) / playlist (空壳留着) /
        release_group_cover_pref / wishlist (用户的想要清单) / schema_migrations

    磁盘上文件不动 — 用户自己用 rm / mv 处理。需 ?confirm=true.
    """
    if not confirm:
        return {"ok": False, "reason": "需要 ?confirm=true"}
    s = get_settings()
    results: dict[str, int] = {}

    # beets DB: 用 sqlalchemy 直接 DELETE, 比 lib.items() 循环快几个数量级
    if s.beets_db.exists():
        from sqlalchemy import create_engine  # noqa: PLC0415
        bengine = create_engine(f"sqlite:///{s.beets_db}")
        with bengine.begin() as bconn:
            for tbl in ("item_attributes", "items", "album_attributes", "albums"):
                r = bconn.execute(text(f"DELETE FROM {tbl}"))
                results[f"beets.{tbl}"] = int(r.rowcount or 0)

    # musictidy DB — 注意 track_fingerprint 不清, 那是用户辛辛苦苦攒下来的
    # 识别历史; 拷新文件回来时 fingerprint worker 用本地指纹直接命中, 不用
    # 重新跑 AcoustID 也能识别 (尤其救中文 / 小众专辑)
    with get_engine().begin() as conn:
        for tbl in (
            "split_suggestion",
            "task_queue",
            "item_decision",
            "pending_decision",
            "playlist_item",
            "transcode_cache",
            "trash_log",
        ):
            try:
                r = conn.execute(text(f"DELETE FROM {tbl}"))
                results[f"musictidy.{tbl}"] = int(r.rowcount or 0)
            except Exception as e:  # noqa: BLE001
                results[f"musictidy.{tbl}"] = f"err: {e}"
    return {"ok": True, "wiped": results,
            "note": "beets lib 已清; 现在拷新文件然后 POST /admin/scan"}


@router.get("/fingerprints/export")
async def fingerprints_export() -> Response:
    """导出本地指纹库到 JSON 文件 (下载)。
    用于备份 / 跨机迁移 / wipe 前留底。
    """
    import json  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    from fastapi.responses import Response as _Response  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415

    from app.db import get_engine  # noqa: PLC0415

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """SELECT recording_mbid, release_group_mbid, fingerprint,
                          duration_s, title, artist, album, source, created_at
                   FROM track_fingerprint
                   ORDER BY created_at"""
            )
        ).all()
    data = {
        "version": 1,
        "exported_at": int(datetime.now(timezone.utc).timestamp()),
        "count": len(rows),
        "fingerprints": [
            {
                "recording_mbid": r.recording_mbid or "",
                "release_group_mbid": r.release_group_mbid or "",
                "fingerprint": r.fingerprint,
                "duration_s": float(r.duration_s or 0),
                "title": r.title or "",
                "artist": r.artist or "",
                "album": r.album or "",
                "source": r.source or "",
                "created_at": int(r.created_at or 0),
            }
            for r in rows
        ],
    }
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    fname = f"musictidy-fingerprints-{data['exported_at']}.json"
    return _Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/fingerprints/import")
async def fingerprints_import(payload: dict) -> dict:
    """导入指纹 JSON (export 出来的格式)。

    body: {"fingerprints": [{recording_mbid, release_group_mbid, fingerprint,
                            duration_s, title, artist, album, source, created_at}, ...]}
    冲突策略: 同 fingerprint 串已存在 → skip; 否则 INSERT (item_id 给个递减的
    占位负数, 不影响真 items).
    """
    import time as _time  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    from app.db import get_engine  # noqa: PLC0415

    rows = payload.get("fingerprints") or []
    if not isinstance(rows, list):
        raise HTTPException(400, detail="payload.fingerprints must be list")

    inserted = 0
    skipped = 0
    # 从最小的 item_id 往下 (负数), 避免跟真 items 撞 (真 items 都是正 autoincrement)
    with get_engine().begin() as conn:
        min_id = conn.execute(
            text("SELECT MIN(item_id) FROM track_fingerprint")
        ).scalar() or 0
        next_id = min(int(min_id), 0) - 1

        for r in rows:
            if not isinstance(r, dict):
                skipped += 1
                continue
            fp = r.get("fingerprint")
            if not fp:
                skipped += 1
                continue
            exists = conn.execute(
                text("SELECT 1 FROM track_fingerprint WHERE fingerprint=:fp LIMIT 1"),
                {"fp": fp},
            ).first()
            if exists:
                skipped += 1
                continue
            conn.execute(
                text(
                    """INSERT INTO track_fingerprint
                           (item_id, recording_mbid, release_group_mbid,
                            fingerprint, duration_s,
                            title, artist, album, source, created_at)
                       VALUES (:id, :rec, :rg, :fp, :dur,
                               :t, :a, :al, :src, :now)"""
                ),
                {
                    "id": next_id,
                    "rec": r.get("recording_mbid") or None,
                    "rg": r.get("release_group_mbid") or None,
                    "fp": fp,
                    "dur": float(r.get("duration_s") or 0),
                    "t": r.get("title") or None,
                    "a": r.get("artist") or None,
                    "al": r.get("album") or None,
                    "src": r.get("source") or "imported",
                    "now": int(r.get("created_at") or _time.time()),
                },
            )
            inserted += 1
            next_id -= 1
    return {"ok": True, "inserted": inserted, "skipped": skipped, "received": len(rows)}


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
