"""Chromaprint 指纹 + AcoustID 查询 → 写回 beets.

依赖：
- fpcalc 二进制（macOS: brew install chromaprint；FreeBSD: pkg install chromaprint）
- AcoustID API key (env: ACOUSTID_API_KEY，免费注册)

任务流：
  payload = {"item_id": int}

  1. 拿 item path
  2. fpcalc → (duration, fingerprint)
  3. acoustid.lookup → recordings + release-groups
  4. 取最高分（score ≥ 0.85）
  5. 写回 beets item.mb_trackid / mb_releasegroupid / mb_artistid /
     mb_albumartistid
  6. enqueue('mb_fetch_artist', {artist_mbid}) 触发 discography 拉取
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import acoustid

from app import beets_bridge
from app.config import get_settings
from app.workers import queue

log = logging.getLogger(__name__)

ACOUSTID_META = "recordings releasegroups"
MIN_SCORE = 0.85

# 一次性 warning 闸 —— 避免每首歌都刷"未配置 API key"
_warned_no_key = False
_warned_no_fpcalc = False


async def handle_fingerprint(payload: dict[str, Any]) -> None:
    """识别一首 item。优先 AcoustID 指纹；无 key / 无匹配 → tag-based MB 搜索 fallback."""
    global _warned_no_key
    s = get_settings()
    item_id = int(payload["item_id"])
    lib = beets_bridge.get_library(s.beets_db, s.music_root)

    # 1. AcoustID 指纹路径
    acoustid_match = None
    if s.acoustid_api_key:
        path = beets_bridge.get_item_path(lib, item_id)
        if path is None:
            log.warning("fingerprint: item %d 不在 beets DB", item_id)
            return
        if path.exists():
            # 拿当前 album tag —— 用来在 AcoustID 返回的多个 release-group
            # 里挑正确的（同一首歌可能出现在单曲+专辑+合集，rg 不一样）
            existing_tags = beets_bridge.get_item_tags(lib, item_id) or {}
            acoustid_match = await _try_acoustid(
                path, s.acoustid_api_key, existing_tags.get("album", ""),
            )
    elif not _warned_no_key:
        log.warning(
            "fingerprint: ACOUSTID_API_KEY 未设置；只走 tag-based MB 搜索 fallback。"
            "填了 key 之后 POST /admin/identify-unidentified 重排"
            "（WAV 这种无 tag 的文件必须靠指纹）。"
        )
        _warned_no_key = True

    if acoustid_match is not None:
        # 把指纹和元数据顺手存进我们自己的指纹库
        from app import fingerprint_db  # noqa: PLC0415
        fp_str = acoustid_match.pop("_fp", None)
        fp_dur = acoustid_match.pop("_duration", None)
        rec_title = acoustid_match.pop("_rec_title", None)
        rec_artist = acoustid_match.pop("_rec_artist", None)
        rec_album = acoustid_match.pop("_rec_album", None)

        _write_back(lib, item_id, source="acoustid", **acoustid_match)

        if fp_str and fp_dur:
            fingerprint_db.save(
                item_id=item_id,
                fingerprint=fp_str,
                duration_s=float(fp_dur),
                recording_mbid=acoustid_match["rec_mbid"],
                title=rec_title,
                artist=rec_artist,
                album=rec_album,
                source="acoustid",
            )
        return

    # 2. Tag-based fallback —— MB 按 artist+album 搜
    tags = beets_bridge.get_item_tags(lib, item_id)
    if not tags or (not tags["artist"] and not tags["albumartist"]):
        log.info("fingerprint: item %d 无指纹匹配也无 artist tag，放弃", item_id)
        return

    from app.workers.musicbrainz import (  # noqa: PLC0415
        search_artist_mbid,
        search_release_by_tags,
    )

    query_artist = tags["albumartist"] or tags["artist"]

    if tags["album"]:
        match = await search_release_by_tags(
            artist=query_artist,
            album=tags["album"],
        )
        if match:
            _write_back(
                lib, item_id, source="tag-search (album)",
                rec_mbid=None,
                rg_mbid=match["releasegroup_mbid"],
                artist_mbid=match["album_artist_mbid"],
                album_artist_mbid=match["album_artist_mbid"],
                score=None,
            )
            return

    # 3. 终极 fallback：只挂艺人级（不知道是哪张专辑）
    artist_mbid = await search_artist_mbid(query_artist)
    if artist_mbid:
        _write_back(
            lib, item_id, source="tag-search (artist-only)",
            rec_mbid=None,
            rg_mbid=None,
            artist_mbid=artist_mbid,
            album_artist_mbid=artist_mbid,
            score=None,
        )
    else:
        log.info(
            "fingerprint: item %d MB 完全无匹配 (artist=%r album=%r)",
            item_id, query_artist, tags["album"],
        )


async def _try_acoustid(path, api_key: str, current_album: str = ""):
    """跑 fpcalc + AcoustID。返回 match dict 或 None；致命错误抛.

    current_album: 文件 tag 里已有的专辑名，用于在 AcoustID 返回的多个
    release-group 候选里挑正确的（同一 recording 可能出现在多张专辑）.
    """
    global _warned_no_fpcalc
    try:
        result = await asyncio.to_thread(
            _blocking_fingerprint_and_lookup, path, api_key, current_album
        )
    except acoustid.NoBackendError:
        if not _warned_no_fpcalc:
            log.error(
                "fingerprint: 找不到 fpcalc；macOS: brew install chromaprint。"
            )
            _warned_no_fpcalc = True
        return None  # 走 tag fallback
    except acoustid.FingerprintGenerationError as e:
        log.warning("fingerprint: fpcalc 失败 %s — %s", path, e)
        return None
    except acoustid.WebServiceError as e:
        log.warning("fingerprint: AcoustID API 错: %s", e)
        raise  # 网络问题 → 重试

    if result is None:
        return None
    (score, rec_mbid, rg_mbid, artist_mbid, album_artist_mbid,
     fp_str, duration, rec_title, rec_artist, rec_album) = result
    return {
        "score": score,
        "rec_mbid": rec_mbid,
        "rg_mbid": rg_mbid,
        "artist_mbid": artist_mbid,
        "_fp": fp_str,
        "_duration": duration,
        "_rec_title": rec_title,
        "_rec_artist": rec_artist,
        "_rec_album": rec_album,
        "album_artist_mbid": album_artist_mbid,
    }


def _write_back(
    lib,
    item_id: int,
    *,
    source: str,
    rec_mbid: str | None,
    rg_mbid: str | None,
    artist_mbid: str | None,
    album_artist_mbid: str | None,
    score: float | None,
) -> None:
    # 如果 mb_artist 缓存里有 canonical 名，顺手覆盖 it.albumartist，
    # organize 才会把目录改成规范名（旧 tag 里的名字否则会一直占着）
    album_artist_name = _canonical_artist_name(album_artist_mbid)
    beets_bridge.set_mb_ids(
        lib, item_id,
        track_mbid=rec_mbid,
        releasegroup_mbid=rg_mbid,
        artist_mbid=artist_mbid,
        album_artist_mbid=album_artist_mbid,
        album_artist=album_artist_name,
    )
    score_str = f" score={score:.2f}" if score is not None else ""
    log.info(
        "fingerprint: item %d identified via %s — rg=%s artist=%s%s",
        item_id, source, rg_mbid, album_artist_mbid, score_str,
    )
    if album_artist_mbid:
        queue.enqueue("mb_fetch_artist", {"artist_mbid": album_artist_mbid})

    # 心愿单 phase 2: fingerprint 写完元数据，看看这张是不是心愿单上的，是的话打勾
    try:
        from app.api.wishlist import _fulfill_matching_wishlist  # noqa: PLC0415
        n = _fulfill_matching_wishlist()
        if n > 0:
            log.info("fingerprint: %d wishlist item(s) auto-fulfilled", n)
    except Exception:
        pass  # fulfilled 失败不阻塞主流程


def _canonical_artist_name(artist_mbid: str | None) -> str | None:
    """从 mb_artist 缓存表查 canonical name，没缓存返回 None.

    第一次识别时缓存可能还没填，会返回 None；这没关系——_write_back
    末尾 enqueue 了 mb_fetch_artist，下次 backfill 或重新识别就能拿到了。
    """
    if not artist_mbid:
        return None
    from sqlalchemy import text  # noqa: PLC0415

    from app.db import get_engine  # noqa: PLC0415

    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT name FROM mb_artist WHERE mbid = :m"),
            {"m": artist_mbid},
        ).first()
    return row[0] if row and row[0] else None


def _blocking_fingerprint_and_lookup(
    path: Path, api_key: str, current_album: str = "",
):
    """阻塞调用 fpcalc + AcoustID HTTP。返回 tuple 或 None。

    tuple 内容:
      (score, rec_mbid, rg_mbid, artist_mbid, album_artist_mbid,
       fp_str, duration, rec_title, rec_artist_name, rec_album_title)
    """
    duration, fp = acoustid.fingerprint_file(str(path))
    fp_str = fp.decode("ascii") if isinstance(fp, bytes) else str(fp)
    response = acoustid.lookup(api_key, fp, duration, meta=ACOUSTID_META)

    if response.get("status") != "ok":
        return None

    results = response.get("results") or []
    if not results:
        return None

    best = max(results, key=lambda r: r.get("score", 0.0))
    score = float(best.get("score", 0.0))
    if score < MIN_SCORE:
        return None

    recordings = best.get("recordings") or []
    if not recordings:
        return None

    rec = recordings[0]
    rec_mbid = rec.get("id")
    rec_title = rec.get("title") or None

    # 录音的 artists（song artist）
    rec_artists = rec.get("artists") or []
    artist_mbid = rec_artists[0].get("id") if rec_artists else None
    rec_artist_name = rec_artists[0].get("name") if rec_artists else None

    # release-group：同一首歌可能出现在多张专辑（单曲 / 专辑 / BEST 合集），
    # AcoustID 把全部 release-group 都返回。用文件 album tag 在候选里挑匹配的。
    rgs = rec.get("releasegroups") or []
    rg_mbid = None
    album_artist_mbid = None
    rec_album_title = None
    if rgs:
        rg = _pick_release_group(rgs, current_album)
        rg_mbid = rg.get("id")
        rec_album_title = rg.get("title") or None
        rg_artists = rg.get("artists") or []
        if rg_artists:
            album_artist_mbid = rg_artists[0].get("id")

    return (
        score, rec_mbid, rg_mbid, artist_mbid, album_artist_mbid or artist_mbid,
        fp_str, float(duration), rec_title, rec_artist_name, rec_album_title,
    )


def _pick_release_group(rgs: list, current_album: str) -> dict:
    """从 AcoustID 给的 release-group 候选里挑跟文件 album tag 匹配的.

    优先级：精确等同 > 互相包含 > 第一个.
    """
    if not current_album or len(rgs) == 1:
        return rgs[0]
    ca = current_album.lower().strip()
    # exact
    for rg in rgs:
        if (rg.get("title") or "").lower().strip() == ca:
            return rg
    # contains
    for rg in rgs:
        t = (rg.get("title") or "").lower().strip()
        if t and (ca in t or t in ca):
            return rg
    return rgs[0]
