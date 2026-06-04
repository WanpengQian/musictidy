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

    # 已经手工 / split 钉死了 rg + recording? 只跑指纹入本地库, 不动 mb_* 字段
    pre_bound_tags = beets_bridge.get_item_tags(lib, item_id) or {}
    pre_bound = bool(
        pre_bound_tags.get("mb_trackid") and pre_bound_tags.get("mb_releasegroupid")
    )

    # 0. .musictidy.json sidecar 优先级最高 — 用户在该目录写了 rg_mbid 就是
    # 这张专辑铁定的事, 跳过 AcoustID 直接按 filename position 钉。
    if not pre_bound:
        sidecar_path = beets_bridge.get_item_path(lib, item_id)
        if sidecar_path and sidecar_path.exists():
            if await _try_bind_from_sidecar(lib, item_id, sidecar_path):
                return

    # 0.5 md5 命中: byte-identical 文件 (移动/重命名/wipe 后 reload) 直接复用,
    # 跳过 fpcalc + AcoustID. md5 流式 ~0.2s, fpcalc ~1-2s, 省 70%+ 时间。
    item_md5: str | None = None
    if not pre_bound:
        path = beets_bridge.get_item_path(lib, item_id)
        if path is not None and path.exists():
            from app import fingerprint_db  # noqa: PLC0415
            item_md5 = await asyncio.to_thread(fingerprint_db.compute_md5, path)
            if item_md5:
                md5_hit = fingerprint_db.lookup_by_md5(item_md5)
                if md5_hit and md5_hit.get("recording_mbid"):
                    log.info(
                        "fingerprint: item %d md5 命中 rec=%s rg=%s, 跳过 fpcalc + AcoustID",
                        item_id, md5_hit.get("recording_mbid"),
                        md5_hit.get("release_group_mbid"),
                    )
                    _write_back(
                        lib, item_id,
                        source=f"md5-cache ({md5_hit.get('source','?')})",
                        rec_mbid=md5_hit.get("recording_mbid"),
                        rg_mbid=md5_hit.get("release_group_mbid") or None,
                        artist_mbid=None,
                        album_artist_mbid=None,
                        score=None,
                    )
                    # 顺手存 (item_id 不同, md5/fp 串都一样)
                    fingerprint_db.save(
                        item_id=item_id,
                        fingerprint=md5_hit.get("fingerprint") or "",
                        duration_s=float(md5_hit.get("duration_s") or 0),
                        recording_mbid=md5_hit.get("recording_mbid"),
                        release_group_mbid=md5_hit.get("release_group_mbid") or None,
                        title=md5_hit.get("title"),
                        artist=md5_hit.get("artist"),
                        album=md5_hit.get("album"),
                        source=md5_hit.get("source") or "md5-cache",
                        md5=item_md5,
                    )
                    return

    # 1. AcoustID 指纹路径
    acoustid_match = None
    fp_str: str | None = None
    fp_dur: float | None = None
    if s.acoustid_api_key:
        path = beets_bridge.get_item_path(lib, item_id)
        if path is None:
            log.warning("fingerprint: item %d 不在 beets DB", item_id)
            return
        if path.exists():
            # 拿当前 album tag —— 用来在 AcoustID 返回的多个 release-group
            # 里挑正确的（同一首歌可能出现在单曲+专辑+合集，rg 不一样）
            fp_str, fp_dur, acoustid_match = await _try_acoustid(
                path, s.acoustid_api_key, pre_bound_tags.get("album", ""),
            )
    elif not _warned_no_key:
        log.warning(
            "fingerprint: ACOUSTID_API_KEY 未设置；只走 tag-based MB 搜索 fallback。"
            "填了 key 之后 POST /admin/identify-unidentified 重排"
            "（WAV 这种无 tag 的文件必须靠指纹）。"
        )
        _warned_no_key = True

    # 0.5 fpcalc 出来的指纹串先查本地 manual 缓存 (优先级高于 AcoustID).
    # 用户手动标的 = 真理, AcoustID 的 _pick_release_group 不该把它覆盖。
    # 跨 wipe 也救 — 同一文件 fp 串字节一致, 老 manual 行能直接被命中。
    if not pre_bound and fp_str:
        from app import fingerprint_db  # noqa: PLC0415
        manual_hit = fingerprint_db.lookup_by_fingerprint(fp_str, manual_only=True)
        if manual_hit and manual_hit.get("recording_mbid"):
            log.info(
                "fingerprint: item %d 本地 manual 缓存命中 rec=%s rg=%s, 跳过 AcoustID",
                item_id, manual_hit.get("recording_mbid"),
                manual_hit.get("release_group_mbid"),
            )
            _write_back(
                lib, item_id, source="manual-cache",
                rec_mbid=manual_hit.get("recording_mbid"),
                rg_mbid=manual_hit.get("release_group_mbid") or None,
                artist_mbid=None,
                album_artist_mbid=None,
                score=None,
            )
            fingerprint_db.save(
                item_id=item_id,
                fingerprint=fp_str,
                duration_s=float(fp_dur or 0),
                recording_mbid=manual_hit.get("recording_mbid"),
                release_group_mbid=manual_hit.get("release_group_mbid") or None,
                title=manual_hit.get("title"),
                artist=manual_hit.get("artist"),
                album=manual_hit.get("album"),
                source="manual",  # 维持 manual 血统
                md5=item_md5,
            )
            return

    if acoustid_match is not None:
        # 把指纹和元数据顺手存进我们自己的指纹库
        from app import fingerprint_db  # noqa: PLC0415
        acoustid_match.pop("_fp", None)
        acoustid_match.pop("_duration", None)
        rec_title = acoustid_match.pop("_rec_title", None)
        rec_artist = acoustid_match.pop("_rec_artist", None)
        rec_album = acoustid_match.pop("_rec_album", None)
        candidate_rgs = acoustid_match.pop("_candidate_rgs", None)

        if pre_bound:
            log.info(
                "fingerprint: item %d 已手动钉死, AcoustID 命中 rec=%s 仅入指纹库",
                item_id, acoustid_match.get("rec_mbid"),
            )
        else:
            _write_back(lib, item_id, source="acoustid", **acoustid_match)

        if fp_str and fp_dur:
            fingerprint_db.save(
                item_id=item_id,
                fingerprint=fp_str,
                duration_s=float(fp_dur),
                recording_mbid=acoustid_match["rec_mbid"],
                release_group_mbid=acoustid_match.get("rg_mbid"),
                title=rec_title,
                artist=rec_artist,
                album=rec_album,
                source="acoustid",
                candidate_rgs=candidate_rgs,
                md5=item_md5,
            )
        return

    # 1.5 AcoustID 没命中 → 查我们自己的本地指纹缓存
    # 上次识别过 / 用户人工 identify 过同一首歌的 fp 都会在这里命中
    # (中文/小众专辑 AcoustID 库里没有时, 这条是唯一指望)
    if not pre_bound and fp_str:
        from app import fingerprint_db  # noqa: PLC0415
        hit = fingerprint_db.lookup_by_fingerprint(fp_str)
        if hit and hit.get("recording_mbid"):
            log.info(
                "fingerprint: item %d 本地指纹缓存命中 rec=%s rg=%s",
                item_id, hit.get("recording_mbid"), hit.get("release_group_mbid"),
            )
            _write_back(
                lib, item_id, source="local-cache",
                rec_mbid=hit.get("recording_mbid"),
                rg_mbid=hit.get("release_group_mbid") or None,
                artist_mbid=None,
                album_artist_mbid=None,
                score=None,
            )
            # 顺手把当前 item 的 fp 也存一份 (item_id 这次不同了, 但 fp 串一样)
            fingerprint_db.save(
                item_id=item_id,
                fingerprint=fp_str,
                duration_s=float(fp_dur or 0),
                recording_mbid=hit.get("recording_mbid"),
                release_group_mbid=hit.get("release_group_mbid") or None,
                title=hit.get("title"),
                artist=hit.get("artist"),
                album=hit.get("album"),
                source="local-cache",
                md5=item_md5,
            )
            return

    # 已手动钉死 → 不走 tag fallback (避免 rg 被冲)
    if pre_bound:
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
    """跑 fpcalc + AcoustID。

    返回 (fp_str, duration, match_dict_or_None);
    fp 跑出来但 AcoustID miss 时 match=None, 调用方可以拿 fp 查本地缓存。
    fpcalc 都跑不动 → 返回 (None, None, None) 走 tag fallback。
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
        return None, None, None
    except acoustid.FingerprintGenerationError as e:
        log.warning("fingerprint: fpcalc 失败 %s — %s", path, e)
        return None, None, None
    except acoustid.WebServiceError as e:
        log.warning("fingerprint: AcoustID API 错: %s", e)
        raise  # 网络问题 → 重试

    if result is None:
        return None, None, None
    # 命中: 11-tuple (新增 candidate_rg_mbids); 未命中: 3-tuple (fp_str, dur, None)
    if len(result) >= 11:
        (score, rec_mbid, rg_mbid, artist_mbid, album_artist_mbid,
         fp_str, duration, rec_title, rec_artist, rec_album,
         candidate_rgs) = result[:11]
        match = {
            "score": score,
            "rec_mbid": rec_mbid,
            "rg_mbid": rg_mbid,
            "artist_mbid": artist_mbid,
            "_fp": fp_str,
            "_duration": duration,
            "_rec_title": rec_title,
            "_rec_artist": rec_artist,
            "_rec_album": rec_album,
            "_candidate_rgs": candidate_rgs,  # 全部候选, 给跨 dir 投票用
            "album_artist_mbid": album_artist_mbid,
        }
        return fp_str, duration, match
    fp_str, duration, _ = result
    return fp_str, duration, None


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

    # 整张专辑做成一个大 FLAC 的检测: 文件 length ≈ rg 总长 + 单文件
    # → 自动入 split_suggestion, 用户在 web 顶栏一键确认拆
    if rg_mbid:
        try:
            from app.api.library import maybe_enqueue_split_suggestion  # noqa: PLC0415
            maybe_enqueue_split_suggestion(item_id, rg_mbid)
        except Exception:
            pass  # 检测失败也不阻塞主流程


async def _try_bind_from_sidecar(lib, item_id: int, path) -> bool:
    """该目录有 .musictidy.json + rg_mbid → 按 filename position 直接绑.

    成功返回 True (主流程 return), 失败 / 没 sidecar / 不合适 → False 让
    主流程继续 (AcoustID 等)。
    """
    import json as _json  # noqa: PLC0415
    import os as _os  # noqa: PLC0415
    import re as _re  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    from app import info_sidecar  # noqa: PLC0415
    from app.db import get_engine  # noqa: PLC0415

    sidecar = info_sidecar.read(path.parent)
    if not sidecar:
        return False
    rg_mbid = (sidecar.get("rg_mbid") or "").strip()
    if not rg_mbid:
        return False

    # 拿 rg 的 canonical tracks (没缓存就拉一次)
    tracks: list[dict] = []
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT tracks_json FROM mb_release_group WHERE mbid=:m"),
            {"m": rg_mbid},
        ).first()
    if row and row.tracks_json:
        try:
            raw = _json.loads(row.tracks_json)
            if isinstance(raw, list):
                tracks = sorted(raw, key=lambda t: int(t.get("position") or 0))
        except (TypeError, ValueError):
            pass
    if not tracks:
        # 同步拉一次, 尽量别让 fingerprint worker 卡住更长
        try:
            from app.api.library import release_group_tracks  # noqa: PLC0415
            sub = await release_group_tracks(rg_mbid)
            tracks = sorted(sub.get("tracks") or [],
                            key=lambda t: int(t.get("position") or 0))
        except Exception:  # noqa: BLE001
            pass

    # filename 提 position. 常见多种格式都试:
    #   "01. xxx.flac"        — 最常见
    #   "Track No 5.ape"     — 老论坛 rip 风格
    #   "Track 5.flac"
    #   "05_xxx.flac"
    fname = _os.path.basename(str(path))
    rec_mbid: str | None = None
    pos: int | None = None
    for pat in (
        r"^\s*(\d{1,2})[.\s_-]",                 # 01. xxx / 01_xxx / 01 xxx / 01-xxx
        r"Track\s*N[o.]?\s*(\d{1,2})",           # Track No 5 / Track No.5 / TrackN5
        r"Track[\s_-]+(\d{1,2})",                # Track 5 / Track_5 / Track-5
    ):
        m = _re.search(pat, fname, _re.I)
        if m:
            pos = int(m.group(1))
            break
    if pos is not None and tracks and 1 <= pos <= len(tracks):
        rec_mbid = tracks[pos - 1].get("recording_mbid") or None

    # 至少 rg 一定能定; rec_mbid 取不到也认了 (用户进 web 再手动匹配曲目)
    _write_back(
        lib, item_id, source="sidecar",
        rec_mbid=rec_mbid,
        rg_mbid=rg_mbid,
        artist_mbid=(sidecar.get("artist_mbid") or "").strip() or None,
        album_artist_mbid=(sidecar.get("artist_mbid") or "").strip() or None,
        score=None,
    )
    log.info(
        "fingerprint: item %d 走 .musictidy.json sidecar rg=%s rec=%s",
        item_id, rg_mbid, (rec_mbid or "")[:8],
    )
    return True


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
    """阻塞调用 fpcalc + AcoustID HTTP。

    返回:
      - 命中 → 10-tuple (score, rec_mbid, rg_mbid, artist_mbid, album_artist_mbid,
                         fp_str, duration, rec_title, rec_artist_name, rec_album_title)
      - 未命中但 fpcalc 成功 → 3-tuple (fp_str, duration, None)
      - fpcalc 失败 → 抛 acoustid.FingerprintGenerationError (调用方 catch)
    """
    duration, fp = acoustid.fingerprint_file(str(path))
    fp_str = fp.decode("ascii") if isinstance(fp, bytes) else str(fp)
    response = acoustid.lookup(api_key, fp, duration, meta=ACOUSTID_META)

    if response.get("status") != "ok":
        return fp_str, duration, None

    results = response.get("results") or []
    if not results:
        return fp_str, duration, None

    best = max(results, key=lambda r: r.get("score", 0.0))
    score = float(best.get("score", 0.0))
    if score < MIN_SCORE:
        return fp_str, duration, None

    recordings = best.get("recordings") or []
    if not recordings:
        return fp_str, duration, None

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
    candidate_rg_mbids: list[str] = [r.get("id") for r in rgs if r.get("id")]
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
        candidate_rg_mbids,
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
