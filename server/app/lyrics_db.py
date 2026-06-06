"""按 recording_mbid 缓存歌词. 用户点开歌词面板才 lazy fetch lrclib;
命中就缓存, 下次秒返。台湾繁体老歌 lrclib 基本 miss, miss 也缓存 (404 sentinel),
免得每次刷新都重抓。
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import text

from app.db import get_engine

log = logging.getLogger(__name__)

_LRCLIB_TIMEOUT = 8.0
_LRCLIB_BASE = "https://lrclib.net/api/get"


def ensure_table() -> None:
    with get_engine().begin() as conn:
        conn.execute(text(
            """CREATE TABLE IF NOT EXISTS track_lyrics (
                recording_mbid TEXT PRIMARY KEY,
                lrc            TEXT,           -- LRC synced text
                plain          TEXT,           -- plain unsynced (lrclib 没 synced 给 plain)
                source         TEXT NOT NULL,  -- 'lrclib' / 'manual' / 'embedded' / 'none'
                fetched_at     INTEGER NOT NULL
            )"""
        ))


def get(recording_mbid: str) -> dict[str, Any] | None:
    if not recording_mbid:
        return None
    with get_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT lrc, plain, source, fetched_at "
            "FROM track_lyrics WHERE recording_mbid=:m"
        ), {"m": recording_mbid}).first()
    if not row:
        return None
    return {
        "lrc": row.lrc or "",
        "plain": row.plain or "",
        "source": row.source,
        "fetched_at": int(row.fetched_at),
    }


def save(
    recording_mbid: str,
    *,
    lrc: str = "",
    plain: str = "",
    source: str,
) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(
            """INSERT INTO track_lyrics
                   (recording_mbid, lrc, plain, source, fetched_at)
               VALUES (:m, :lrc, :plain, :src, :ts)
               ON CONFLICT(recording_mbid) DO UPDATE SET
                   lrc=excluded.lrc, plain=excluded.plain,
                   source=excluded.source, fetched_at=excluded.fetched_at"""
        ), {
            "m": recording_mbid,
            "lrc": lrc or "",
            "plain": plain or "",
            "src": source,
            "ts": int(time.time()),
        })


async def fetch_lrclib(
    *, artist: str, title: str, album: str = "", duration_s: float = 0.0,
) -> dict[str, str] | None:
    """命中返回 {lrc, plain, source='lrclib'}; 404/网络错返回 None."""
    if not artist or not title:
        return None
    params = {
        "artist_name": artist,
        "track_name": title,
    }
    if album:
        params["album_name"] = album
    if duration_s and duration_s > 0:
        params["duration"] = str(int(round(duration_s)))
    url = _LRCLIB_BASE + "?" + urlencode(params)
    try:
        async with httpx.AsyncClient(timeout=_LRCLIB_TIMEOUT) as client:
            resp = await client.get(url, headers={
                "User-Agent": "MusicTidy/0.1 (https://musictidy.com)",
            })
    except httpx.RequestError as e:
        log.warning("lrclib fetch error: %s", e)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        log.warning("lrclib HTTP %d for %s - %s", resp.status_code, artist, title)
        return None
    try:
        d = resp.json()
    except Exception:  # noqa: BLE001
        return None
    lrc = (d.get("syncedLyrics") or "").strip()
    plain = (d.get("plainLyrics") or "").strip()
    if not lrc and not plain:
        return None
    return {"lrc": lrc, "plain": plain, "source": "lrclib"}
