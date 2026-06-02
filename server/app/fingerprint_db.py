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
    """启动时调一次。CREATE TABLE IF NOT EXISTS。"""
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """CREATE TABLE IF NOT EXISTS track_fingerprint (
                    item_id          INTEGER PRIMARY KEY,
                    recording_mbid   TEXT,
                    fingerprint      TEXT NOT NULL,
                    duration_s       REAL NOT NULL,
                    title            TEXT,
                    artist           TEXT,
                    album            TEXT,
                    source           TEXT NOT NULL,
                    created_at       INTEGER NOT NULL
                )"""
            )
        )
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
) -> None:
    """INSERT OR REPLACE 一条指纹记录."""
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO track_fingerprint
                       (item_id, recording_mbid, fingerprint, duration_s,
                        title, artist, album, source, created_at)
                   VALUES (:id, :rec, :fp, :dur, :t, :a, :al, :src, :now)
                   ON CONFLICT(item_id) DO UPDATE SET
                       recording_mbid = excluded.recording_mbid,
                       fingerprint    = excluded.fingerprint,
                       duration_s     = excluded.duration_s,
                       title          = excluded.title,
                       artist         = excluded.artist,
                       album          = excluded.album,
                       source         = excluded.source"""
            ),
            {
                "id": item_id,
                "rec": recording_mbid,
                "fp": fingerprint,
                "dur": duration_s,
                "t": title, "a": artist, "al": album,
                "src": source,
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
) -> bool:
    """跑 fpcalc 提指纹再 save。失败返回 False（不抛）."""
    try:
        import acoustid  # noqa: PLC0415
        duration, fp = acoustid.fingerprint_file(str(path))
    except Exception as e:  # noqa: BLE001
        log.warning("fingerprint_db: extract failed for %s: %s", path.name, e)
        return False
    save(
        item_id=item_id,
        fingerprint=fp.decode("ascii") if isinstance(fp, bytes) else str(fp),
        duration_s=float(duration),
        recording_mbid=recording_mbid,
        title=title, artist=artist, album=album,
        source=source,
    )
    return True


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
