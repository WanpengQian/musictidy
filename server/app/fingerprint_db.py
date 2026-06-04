"""自家指纹库.

每次从外部（AcoustID / 用户人工）拿到一个 (item, recording_mbid) 对，就把
chromaprint 指纹连同 metadata 落库一份。下一轮可以做"先查本地，没命中再
走外部"，把 AcoustID/MB 流量降下来。

格式：直接存 chromaprint 的 base64 字符串（pyacoustid 给的 fp 就是那种），
匹配算法等下一轮加。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db import get_engine

log = logging.getLogger(__name__)


def ensure_table() -> None:
    """启动时调一次。CREATE TABLE IF NOT EXISTS + 缺啥列就 ALTER 补。

    新列 (release_group_mbid / candidate_rgs / md5) 一并写进 CREATE TABLE,
    fresh wipe 后第一次启动就齐; 已有旧 DB 走 ALTER 兜底, 不靠 migration
    二次重启。
    """
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """CREATE TABLE IF NOT EXISTS track_fingerprint (
                    item_id            INTEGER PRIMARY KEY,
                    recording_mbid     TEXT,
                    release_group_mbid TEXT,
                    fingerprint        TEXT NOT NULL,
                    duration_s         REAL NOT NULL,
                    title              TEXT,
                    artist             TEXT,
                    album              TEXT,
                    source             TEXT NOT NULL,
                    candidate_rgs      TEXT,
                    md5                TEXT,
                    created_at         INTEGER NOT NULL
                )"""
            )
        )
        cols = {r[1] for r in conn.execute(
            text("PRAGMA table_info(track_fingerprint)")
        ).all()}
        for col_name, col_type in (
            ("release_group_mbid", "TEXT"),
            ("candidate_rgs", "TEXT"),
            ("md5", "TEXT"),
        ):
            if col_name not in cols:
                conn.execute(text(
                    f"ALTER TABLE track_fingerprint ADD COLUMN {col_name} {col_type}"
                ))
        conn.execute(
            text(
                """CREATE INDEX IF NOT EXISTS idx_fp_duration
                   ON track_fingerprint(duration_s)"""
            )
        )
        conn.execute(
            text(
                """CREATE INDEX IF NOT EXISTS idx_fp_recording
                   ON track_fingerprint(recording_mbid)"""
            )
        )
        conn.execute(
            text(
                """CREATE INDEX IF NOT EXISTS idx_fp_md5
                   ON track_fingerprint(md5)"""
            )
        )


def compute_md5(path: Path) -> str | None:
    """流式计算文件 md5. 失败返回 None (不抛). ~0.2s/100MB FLAC."""
    import hashlib  # noqa: PLC0415
    h = hashlib.md5()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        log.warning("fingerprint_db: md5 失败 %s: %s", path, e)
        return None


def save(
    *,
    item_id: int,
    fingerprint: str,
    duration_s: float,
    recording_mbid: str | None,
    title: str | None,
    artist: str | None,
    album: str | None,
    source: str,
    release_group_mbid: str | None = None,
    candidate_rgs: list[str] | None = None,
    md5: str | None = None,
) -> None:
    """INSERT OR REPLACE 一条指纹记录.

    重点: 已经有一条 source='manual' 的记录, 新来的 source != 'manual'
    就 **不覆盖** — 用户手动绑定的成果不能被后续 AcoustID 自动覆盖回去。
    """
    with get_engine().begin() as conn:
        # 看看这个 item 当前缓存源
        existing = conn.execute(
            text("SELECT source FROM track_fingerprint WHERE item_id=:i"),
            {"i": item_id},
        ).first()
        if existing and existing.source == "manual" and source != "manual":
            return  # 不动 manual 记录
        import json as _json  # noqa: PLC0415
        cand_json = _json.dumps(candidate_rgs) if candidate_rgs else None
        conn.execute(
            text(
                """INSERT INTO track_fingerprint
                       (item_id, recording_mbid, release_group_mbid,
                        fingerprint, duration_s,
                        title, artist, album, source, candidate_rgs, md5,
                        created_at)
                   VALUES (:id, :rec, :rg, :fp, :dur,
                           :t, :a, :al, :src, :cand, :md5, :now)
                   ON CONFLICT(item_id) DO UPDATE SET
                       recording_mbid     = excluded.recording_mbid,
                       release_group_mbid = excluded.release_group_mbid,
                       fingerprint        = excluded.fingerprint,
                       duration_s         = excluded.duration_s,
                       title              = excluded.title,
                       artist             = excluded.artist,
                       album              = excluded.album,
                       source             = excluded.source,
                       candidate_rgs      = COALESCE(excluded.candidate_rgs,
                                                    track_fingerprint.candidate_rgs),
                       md5                = COALESCE(excluded.md5,
                                                    track_fingerprint.md5)"""
            ),
            {
                "id": item_id,
                "rec": recording_mbid,
                "rg": release_group_mbid,
                "fp": fingerprint,
                "dur": duration_s,
                "t": title, "a": artist, "al": album,
                "src": source,
                "cand": cand_json,
                "md5": md5,
                "now": int(time.time()),
            },
        )


def extract_and_save(
    item_id: int,
    path: Path,
    *,
    recording_mbid: str | None,
    title: str | None,
    artist: str | None,
    album: str | None,
    source: str,
    release_group_mbid: str | None = None,
) -> bool:
    """跑 fpcalc 提指纹再 save。失败返回 False（不抛）.

    同时算 md5 (跨副本 byte-identical 匹配用) — 比 fp 便宜很多, 顺手算了。
    """
    try:
        import acoustid  # noqa: PLC0415
        duration, fp = acoustid.fingerprint_file(str(path))
    except Exception as e:  # noqa: BLE001
        log.warning("fingerprint_db: extract failed for %s: %s", path.name, e)
        return False
    md5 = compute_md5(path)
    save(
        item_id=item_id,
        fingerprint=fp.decode("ascii") if isinstance(fp, bytes) else str(fp),
        duration_s=float(duration),
        recording_mbid=recording_mbid,
        title=title, artist=artist, album=album,
        source=source,
        release_group_mbid=release_group_mbid,
        md5=md5,
    )
    return True


def lookup_by_md5(md5: str) -> dict[str, Any] | None:
    """精确匹配 md5 (byte-identical 文件), 命中返回 metadata。

    用法: fingerprint worker 第一步算 md5 → 这里查命中就跳过 fpcalc + AcoustID
    (移动/重命名/wipe 后 reload 都靠这条秒级恢复). 优先 manual > 其他.
    """
    if not md5:
        return None
    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                """SELECT recording_mbid, release_group_mbid,
                          title, artist, album, source, duration_s, fingerprint
                   FROM track_fingerprint
                   WHERE md5 = :m
                   ORDER BY (source = 'manual') DESC,
                            (recording_mbid IS NOT NULL AND recording_mbid != '') DESC
                   LIMIT 1"""
            ),
            {"m": md5},
        ).first()
    if not row:
        return None
    return {
        "recording_mbid": row.recording_mbid or "",
        "release_group_mbid": row.release_group_mbid or "",
        "title": row.title,
        "artist": row.artist,
        "album": row.album,
        "source": row.source,
        "duration_s": float(row.duration_s or 0),
        "fingerprint": row.fingerprint or "",
    }


def lookup_by_fingerprint(
    fp_str: str, *, manual_only: bool = False,
) -> dict[str, Any] | None:
    """精确匹配指纹字符串 (同一文件 fpcalc 出来字节一致), 命中返回 metadata.

    用法:
    - manual_only=True: 只看 source='manual' 的记录 → 用户钉死的真理,
      worker 在 AcoustID 之前查一次, 命中就跳过 AcoustID (避免 AcoustID
      把用户的人工选择覆盖回去).
    - manual_only=False: 任何 source 都行, 优先 manual > 其他, 优先有
      recording 的 → AcoustID miss 时的兜底回退.
    """
    if not fp_str:
        return None
    where = "fingerprint = :fp"
    if manual_only:
        where += " AND source = 'manual'"
    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                f"""SELECT recording_mbid, release_group_mbid,
                          title, artist, album, source, duration_s
                   FROM track_fingerprint
                   WHERE {where}
                   ORDER BY (source = 'manual') DESC,
                            (recording_mbid IS NOT NULL AND recording_mbid != '') DESC
                   LIMIT 1"""
            ),
            {"fp": fp_str},
        ).first()
    if not row:
        return None
    return {
        "recording_mbid": row.recording_mbid or "",
        "release_group_mbid": row.release_group_mbid or "",
        "title": row.title,
        "artist": row.artist,
        "album": row.album,
        "source": row.source,
        "duration_s": float(row.duration_s or 0),
    }


def stats() -> dict[str, Any]:
    with get_engine().connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM track_fingerprint")).scalar() or 0
        by_source = {
            r.source: int(r.c)
            for r in conn.execute(
                text("SELECT source, COUNT(*) AS c FROM track_fingerprint GROUP BY source")
            ).all()
        }
    return {"total": int(total), "by_source": by_source}
