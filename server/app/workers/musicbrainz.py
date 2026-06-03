"""MusicBrainz API 拉取 + 缓存.

关键约束：
- 全局 1 req/sec（asyncio.Lock + sleep 1.05s 后端余量）
- UA 必须带联系方式（settings.mb_user_agent）
- 数据 CC0，可缓存

任务流：
  payload = {"artist_mbid": str}

  1. 若 mb_artist.stale_after > now → 跳过（已新鲜）
  2. get_artist_by_id(mbid, includes=['release-groups'])
  3. upsert mb_artist
  4. upsert mb_release_group 每一行

不在此 worker 里：
- 拉 release tracklist（用于 track 级 partial 判定，P1 后期再加）
- 抓封面（cover_fetch 独立任务）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import musicbrainzngs
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine

log = logging.getLogger(__name__)

WEEK_SEC = 7 * 24 * 3600
RATE_LIMIT_SEC = 1.05

_lock = asyncio.Lock()
_last_call_ts: float = 0.0
_ua_configured = False


def _configure_user_agent() -> None:
    global _ua_configured
    if _ua_configured:
        return
    s = get_settings()
    # mb_user_agent 形如: "MusicTidy/0.1 ( email@example.com )"
    musicbrainzngs.set_useragent("MusicTidy", "0.1", s.mb_user_agent)
    _ua_configured = True


async def _rate_limited(fn, *args, **kwargs):
    global _last_call_ts
    async with _lock:
        elapsed = time.monotonic() - _last_call_ts
        if elapsed < RATE_LIMIT_SEC:
            await asyncio.sleep(RATE_LIMIT_SEC - elapsed)
        result = await asyncio.to_thread(fn, *args, **kwargs)
        _last_call_ts = time.monotonic()
        return result


def _is_fresh(artist_mbid: str) -> bool:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT stale_after FROM mb_artist WHERE mbid=:m"),
            {"m": artist_mbid},
        ).first()
        if not row:
            return False
        return int(row.stale_after) > int(time.time())


def _upsert_artist(mbid: str, a: dict[str, Any]) -> None:
    now = int(time.time())
    # 拿 genres + tags 投票数，合并 dedup 后存进 mb_artist.genres
    # MB 返回结构: {"genre-list": [{"name": "rock", "count": "12"}, ...],
    #              "tag-list":   [{"name": "alternative rock", "count": "5"}, ...]}
    bag: dict[str, int] = {}
    for src_key in ("genre-list", "tag-list"):
        for item in a.get(src_key) or []:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").lower().strip()
            if not name:
                continue
            try:
                count = int(item.get("count") or 1)
            except (ValueError, TypeError):
                count = 1
            bag[name] = bag.get(name, 0) + count
    # 按 count 降序，留前 20
    ranked = sorted(bag.items(), key=lambda x: -x[1])[:20]
    genres_json = json.dumps(
        [{"name": n, "count": c} for n, c in ranked],
        ensure_ascii=False,
    )

    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO mb_artist
                       (mbid, name, sort_name, country, disambiguation,
                        fetched_at, stale_after, genres)
                   VALUES (:m, :n, :s, :c, :d, :f, :sa, :g)
                   ON CONFLICT(mbid) DO UPDATE SET
                       name=excluded.name,
                       sort_name=excluded.sort_name,
                       country=excluded.country,
                       disambiguation=excluded.disambiguation,
                       fetched_at=excluded.fetched_at,
                       stale_after=excluded.stale_after,
                       genres=excluded.genres"""
            ),
            {
                "m": mbid,
                "n": a.get("name", ""),
                "s": a.get("sort-name", ""),
                "c": a.get("country") or "",
                "d": a.get("disambiguation") or "",
                "f": now,
                "sa": now + WEEK_SEC,
                "g": genres_json,
            },
        )


def _upsert_release_group(rg: dict[str, Any], artist_mbid: str) -> None:
    secondary = rg.get("secondary-type-list") or []
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO mb_release_group
                       (mbid, artist_mbid, title, primary_type,
                        secondary_types, first_release_date, cover_url)
                   VALUES (:m, :a, :t, :pt, :st, :frd, NULL)
                   ON CONFLICT(mbid) DO UPDATE SET
                       artist_mbid=excluded.artist_mbid,
                       title=excluded.title,
                       primary_type=excluded.primary_type,
                       secondary_types=excluded.secondary_types,
                       first_release_date=excluded.first_release_date"""
            ),
            {
                "m": rg["id"],
                "a": artist_mbid,
                "t": rg.get("title", ""),
                "pt": rg.get("type") or "",
                "st": json.dumps(secondary),
                "frd": rg.get("first-release-date") or "",
            },
        )


def _artist_matches(release: dict[str, Any], query_artist: str) -> bool:
    """release.artist-credit 里至少一个 artist 名字跟 query 互相包含。

    MB Lucene 搜索对 CJK 名字很弱，必须 post-hoc 验证，否则会跨艺人误识别
    (e.g. '眉飞色舞' 同名 → Sammi Cheng vs 女子十二乐坊)。
    """
    q = (query_artist or "").lower().strip()
    if not q:
        return False
    for credit in release.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist") or {}
        for field in ("name", "sort-name"):
            name = (artist.get(field) or "").lower()
            if name and (q in name or name in q):
                return True
        for alias in artist.get("alias-list") or []:
            if isinstance(alias, dict):
                a_name = (alias.get("alias") or "").lower()
                if a_name and (q in a_name or a_name in q):
                    return True
    return False


async def search_release_by_tags(
    artist: str, album: str, *, min_score: int = 85
) -> dict[str, str | None] | None:
    """按 artist + album tag 在 MB 搜索 release. 返回最佳 match 的 mbid 集合或 None.

    防误识别：strict=True 强制两个字段都要匹配；返回后再 post-hoc
    校验 artist 实际匹配。
    """
    if not artist or not album:
        return None
    _configure_user_agent()
    try:
        data = await _rate_limited(
            musicbrainzngs.search_releases,
            artist=artist, release=album, limit=5, strict=True,
        )
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as e:
        log.warning("mb search %s / %s 失败: %s", artist, album, e)
        return None

    releases = data.get("release-list") or []
    if not releases:
        return None

    # 必须 artist 真的匹配
    candidates = [r for r in releases if _artist_matches(r, artist)]
    if not candidates:
        log.info(
            "mb search: %d hits for album=%r 但 artist=%r 不匹配，丢弃",
            len(releases), album, artist,
        )
        return None

    best = max(candidates, key=lambda r: int(r.get("ext:score", 0)))
    if int(best.get("ext:score", 0)) < min_score:
        log.info(
            "mb search: artist+album 都对但 score=%s < %d，丢弃",
            best.get("ext:score"), min_score,
        )
        return None

    rg = best.get("release-group") or {}
    credit = best.get("artist-credit") or []
    artist_mbid = None
    if credit and isinstance(credit[0], dict):
        artist_mbid = (credit[0].get("artist") or {}).get("id")

    return {
        "release_mbid": best.get("id"),
        "releasegroup_mbid": rg.get("id"),
        "album_artist_mbid": artist_mbid,
    }


def _name_matches(entity: dict[str, Any], query: str) -> bool:
    q = (query or "").lower().strip()
    if not q:
        return False
    for field in ("name", "sort-name"):
        n = (entity.get(field) or "").lower()
        if n and (q in n or n in q):
            return True
    for alias in entity.get("alias-list") or []:
        if isinstance(alias, dict):
            a = (alias.get("alias") or "").lower()
            if a and (q in a or a in q):
                return True
    return False


async def search_artist_mbid(artist: str, *, min_score: int = 85) -> str | None:
    """只按 artist 名搜 MB，返回 MBID 或 None.

    用作终极 fallback：album 在 MB 里没有，但 artist 有 → 至少把艺人挂上去.
    """
    if not artist:
        return None
    _configure_user_agent()
    try:
        data = await _rate_limited(
            musicbrainzngs.search_artists,
            artist=artist, limit=5, strict=True,
        )
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as e:
        log.warning("mb artist search %s 失败: %s", artist, e)
        return None
    artists = data.get("artist-list") or []
    matching = [a for a in artists if _name_matches(a, artist)]
    if not matching:
        return None
    best = max(matching, key=lambda a: int(a.get("ext:score", 0)))
    if int(best.get("ext:score", 0)) < min_score:
        return None
    return best.get("id")


async def handle_fetch_artist(payload: dict[str, Any]) -> None:
    artist_mbid = str(payload["artist_mbid"])

    if _is_fresh(artist_mbid):
        log.debug("mb: %s 仍新鲜，跳过", artist_mbid)
        return

    _configure_user_agent()
    try:
        # musicbrainzngs lib 的 VALID_INCLUDES 没收 "genres"（虽然 MB API 支持 inc=genres）。
        # 之前传 "genres" 会直接 InvalidIncludeError 让**所有** mb_fetch_artist 集体失败。
        # 走 "tags" 就够 —— _upsert_artist 已经合并 genre-list + tag-list。
        data = await _rate_limited(
            musicbrainzngs.get_artist_by_id,
            artist_mbid,
            includes=["release-groups", "tags"],
        )
    except musicbrainzngs.ResponseError as e:
        # 404 / 错的 mbid —— 不重试
        log.warning("mb: %s 不存在或响应错: %s", artist_mbid, e)
        return
    except musicbrainzngs.NetworkError as e:
        log.warning("mb: %s 网络错: %s", artist_mbid, e)
        raise  # 重试

    artist = data.get("artist") or {}
    rgs = artist.get("release-group-list") or []

    _upsert_artist(artist_mbid, artist)
    for rg in rgs:
        _upsert_release_group(rg, artist_mbid)

    log.info(
        "mb: %s (%s) → %d release-groups",
        artist.get("name", "?"), artist_mbid, len(rgs),
    )


# ── Test helper ─────────────────────────────────────────────────
def _reset_for_tests() -> None:
    global _last_call_ts, _ua_configured
    _last_call_ts = 0.0
    _ua_configured = False
