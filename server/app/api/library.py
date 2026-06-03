"""库浏览 / dashboard JSON endpoints (P1).

数据模型：
- 一个 artist 出现在 dashboard 上的条件：beets 里至少一个 item 挂了它的 MBID
- "拥有"一张专辑 = 至少一个 item 的 mb_releasegroupid 等于这个 release-group
- 完整度 = 拥有数 / 总数 (在指定的 primary_type 过滤下)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path as PPath
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import FileResponse, Response
from sqlalchemy import text

from app.db import get_engine

router = APIRouter()

SORT_CLAUSES: dict[str, str] = {
    "completeness": "completeness IS NULL, completeness DESC, owned DESC",
    "owned": "owned DESC, total DESC",
    "alpha": "sort_name IS NULL, sort_name ASC",
    "items": "items_count DESC",
}

# MusicBrainz 的"群星合集"虚拟艺人，不算独立艺人
VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"


def _canonical_artist_name(artist_mbid: str | None) -> str | None:
    """从 mb_artist 缓存表查 canonical name，没缓存返回 None."""
    if not artist_mbid:
        return None
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT name FROM mb_artist WHERE mbid = :m"),
            {"m": artist_mbid},
        ).first()
    return row[0] if row and row[0] else None


@router.get("/search/suggest")
async def search_suggest(q: str = "", limit_full: int = 6) -> dict:
    """递进搜索：

    - q 空 → 返回库里所有艺人名的「首字」chips + 几个推荐
    - q 非空 → 找前缀匹配的艺人/专辑/曲目；再算所有匹配名字的「下一字」chips

    iOS：q 短时给用户点 chips 接力，匹配少时直接展示全名.
    """
    with get_engine().connect() as conn:
        # 艺人前缀匹配
        if q:
            artist_rows = conn.execute(
                text(
                    """SELECT DISTINCT mbid, name, sort_name, country
                       FROM mb_artist
                       WHERE (name LIKE :p OR sort_name LIKE :p)
                         AND mbid != :va
                       ORDER BY name
                       LIMIT 50"""
                ),
                {"p": f"{q}%", "va": VARIOUS_ARTISTS_MBID},
            ).all()
        else:
            artist_rows = conn.execute(
                text(
                    """SELECT DISTINCT a.mbid, a.name, a.sort_name, a.country
                       FROM mb_artist a
                       JOIN beets.items i
                         ON COALESCE(NULLIF(i.mb_albumartistid,''), i.mb_artistid) = a.mbid
                       WHERE a.mbid != :va
                       ORDER BY a.name"""
                ),
                {"va": VARIOUS_ARTISTS_MBID},
            ).all()

        # 专辑前缀匹配
        if q:
            album_rows = conn.execute(
                text(
                    """SELECT mbid, title, artist_mbid, first_release_date, cover_url
                       FROM mb_release_group
                       WHERE title LIKE :p
                       ORDER BY first_release_date IS NULL, first_release_date
                       LIMIT 50"""
                ),
                {"p": f"{q}%"},
            ).all()
        else:
            album_rows = []

        # 曲目前缀匹配（beets）—— 多带 length / bitrate / mb_releasegroupid / mb_trackid
        # 让 iOS 点了立刻有时长 + 能跳到正确专辑详情
        if q:
            track_rows = conn.execute(
                text(
                    """SELECT id, title, artist, album, format,
                              length, bitrate, mb_releasegroupid, mb_trackid
                       FROM beets.items
                       WHERE title LIKE :p
                       LIMIT 30"""
                ),
                {"p": f"{q}%"},
            ).all()
        else:
            track_rows = []

    # 下一字（去重 + 排序）
    next_chars: set[str] = set()
    q_len = len(q)
    for r in artist_rows:
        for fld in (r.name, r.sort_name):
            if fld and len(fld) > q_len and fld.startswith(q if q else ""):
                next_chars.add(fld[q_len])
    for r in album_rows:
        if r.title and len(r.title) > q_len and r.title.startswith(q):
            next_chars.add(r.title[q_len])
    for r in track_rows:
        if r.title and len(r.title) > q_len and r.title.startswith(q):
            next_chars.add(r.title[q_len])

    def _serialize_artist(r):
        return {"mbid": r.mbid, "name": r.name,
                "sort_name": r.sort_name, "country": r.country}

    def _serialize_album(r):
        return {"mbid": r.mbid, "title": r.title,
                "artist_mbid": r.artist_mbid,
                "first_release_date": r.first_release_date,
                "cover_url": r.cover_url or
                    f"https://coverartarchive.org/release-group/{r.mbid}/front-500"}

    def _serialize_track(r):
        return {
            "id": r.id, "title": r.title,
            "artist": r.artist, "album": r.album, "format": r.format,
            "length": float(r.length or 0),
            "bitrate_kbps": int((r.bitrate or 0) // 1000),
            "mb_releasegroupid": r.mb_releasegroupid or "",
            "mb_trackid": r.mb_trackid or "",
        }

    return {
        "query": q,
        "next_chars": sorted(next_chars),
        "artists": [_serialize_artist(r) for r in artist_rows[:limit_full]],
        "albums": [_serialize_album(r) for r in album_rows[:limit_full]],
        "tracks": [_serialize_track(r) for r in track_rows[:limit_full]],
        "artists_total": len(artist_rows),
        "albums_total": len(album_rows),
        "tracks_total": len(track_rows),
    }


def _list_artists_rows(
    sort: str,
    filter_kind: str,
    primary_types: tuple[str, ...] = ("Album", "EP"),
) -> list[dict[str, Any]]:
    order_by = SORT_CLAUSES.get(sort, SORT_CLAUSES["completeness"])

    pt_placeholders = ",".join(f":pt{i}" for i in range(len(primary_types)))
    pt_params = {f"pt{i}": t for i, t in enumerate(primary_types)}

    sql = f"""
    WITH lib_artists AS (
        SELECT
            COALESCE(NULLIF(mb_albumartistid, ''), mb_artistid) AS mbid,
            COUNT(*) AS items_count,
            -- 文件 tag 里的 albumartist 作为兜底名字。MB 还没 fetch 时立刻
            -- 显示出来；mb_artist.name 一旦拉到就会覆盖。
            MAX(COALESCE(NULLIF(albumartist, ''), artist)) AS raw_name
        FROM beets.items
        WHERE COALESCE(NULLIF(mb_albumartistid, ''), mb_artistid) != ''
          AND COALESCE(NULLIF(mb_albumartistid, ''), mb_artistid) != '{VARIOUS_ARTISTS_MBID}'
        GROUP BY 1
    ),
    rg_counts AS (
        SELECT
            rg.artist_mbid AS mbid,
            COUNT(DISTINCT rg.mbid) AS total,
            COUNT(DISTINCT CASE WHEN owned_rg.mbid IS NOT NULL THEN rg.mbid END) AS owned
        FROM mb_release_group rg
        LEFT JOIN (
            SELECT DISTINCT mb_releasegroupid AS mbid FROM beets.items
            WHERE mb_releasegroupid != ''
        ) owned_rg ON owned_rg.mbid = rg.mbid
        WHERE rg.primary_type IN ({pt_placeholders})
          AND (rg.secondary_types IS NULL OR rg.secondary_types = '[]')
        GROUP BY rg.artist_mbid
    )
    SELECT
        la.mbid,
        la.items_count,
        COALESCE(rc.total, 0)  AS total,
        COALESCE(rc.owned, 0)  AS owned,
        CASE WHEN COALESCE(rc.total, 0) > 0
             THEN CAST(COALESCE(rc.owned, 0) AS REAL) / rc.total
             ELSE NULL END     AS completeness,
        COALESCE(NULLIF(a.name, ''), la.raw_name) AS name,
        COALESCE(NULLIF(a.sort_name, ''), la.raw_name) AS sort_name,
        a.country,
        -- 让 iOS 端能区分"已是 MB 名"和"还在 fetch 中的临时名"，等 fetch 完会自动覆盖
        (a.name IS NULL OR a.name = '') AS mb_pending
    FROM lib_artists la
    LEFT JOIN rg_counts rc ON rc.mbid = la.mbid
    LEFT JOIN mb_artist a ON a.mbid = la.mbid
    ORDER BY {order_by}
    """

    try:
        with get_engine().connect() as conn:
            rows = conn.execute(text(sql), pt_params).all()
    except Exception:
        # beets DB 还不存在 / 表还没建 —— dashboard 显示空状态
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        m = dict(r._mapping)
        # filter 在 Python 层做，简单可靠
        if filter_kind == "incomplete" and m["completeness"] is not None and m["completeness"] >= 1.0:
            continue
        if filter_kind == "complete" and (m["completeness"] is None or m["completeness"] < 1.0):
            continue
        if filter_kind == "no_mb_cache" and m["name"] is not None:
            continue
        # SQLite 把 BOOLEAN 表达式返回为 0/1 int —— iOS Swift Bool decoder 不吃，转成 bool
        if "mb_pending" in m:
            m["mb_pending"] = bool(m["mb_pending"])
        out.append(m)

    # 末尾追加"未识别"虚拟艺人 —— 完全没 mb_artistid/mb_albumartistid 的 items
    # （fingerprint 全失败、beets 没回填 tag）。让用户看得到这堆，能去整理
    try:
        with get_engine().connect() as conn:
            orphan_count = conn.execute(text(
                """SELECT COUNT(*) FROM beets.items
                   WHERE COALESCE(NULLIF(mb_albumartistid, ''), mb_artistid) = ''"""
            )).scalar() or 0
        if int(orphan_count) > 0 and filter_kind in ("all", "incomplete", "no_mb_cache"):
            out.append({
                "mbid": NO_ARTIST_MBID,
                "items_count": int(orphan_count),
                "total": 0,
                "owned": 0,
                "completeness": None,
                "name": "",  # 让 client 按 locale 渲染
                "sort_name": "~~~",
                "country": "",
                "mb_pending": False,
            })
    except Exception:
        pass

    return out


@router.get("/artists")
async def list_artists(
    sort: str = Query("completeness", regex="^(completeness|owned|alpha|items)$"),
    filter: str = Query("all", regex="^(all|incomplete|complete|no_mb_cache)$"),
) -> dict:
    rows = _list_artists_rows(sort, filter)
    return {"sort": sort, "filter": filter, "count": len(rows), "artists": rows}


# 合成"未识别艺人"sentinel：items 完全没 mb_artistid/mb_albumartistid 的会
# 在艺人列表末尾出现一个虚拟艺人，点进去就是按目录聚合的兜底专辑。
NO_ARTIST_MBID = "__orphan__"


FORMAT_PRIORITY: dict[str, int] = {
    # 较小值 = 更优（lossless 优先；同类按 bitrate 比）
    "WAVE": 1, "WAV": 1,
    "FLAC": 2, "ALAC": 2, "APE": 3,
    "AIFF": 2, "WV": 3,
    "MP3": 5, "AAC": 5, "M4A": 5, "OGG": 5, "OPUS": 5,
}


def _list_duplicate_groups() -> list[dict[str, Any]]:
    """按 (mb_trackid, mb_releasegroupid) 双维找重复。

    关键：同一首歌出现在不同专辑（单曲版 / 专辑版 / BEST 合集）是**故意**的，
    不算重复；只在**同一 release-group 里**同曲多文件才是真冗余（一般是
    FLAC + MP3 跨格式）。
    """
    with get_engine().connect() as conn:
        groups = conn.execute(
            text(
                """SELECT mb_trackid, mb_releasegroupid, COUNT(*) AS cnt
                   FROM beets.items
                   WHERE mb_trackid != '' AND mb_releasegroupid != ''
                   GROUP BY mb_trackid, mb_releasegroupid
                   HAVING cnt > 1
                   ORDER BY cnt DESC, mb_trackid"""
            )
        ).all()

        out: list[dict[str, Any]] = []
        for g in groups:
            files = conn.execute(
                text(
                    """SELECT id, path, format, bitrate, samplerate, length,
                              title, artist, album
                       FROM beets.items
                       WHERE mb_trackid = :t AND mb_releasegroupid = :rg"""
                ),
                {"t": g.mb_trackid, "rg": g.mb_releasegroupid},
            ).all()

            files_dicts = [dict(f._mapping) for f in files]
            for f in files_dicts:
                f["path"] = bytes(f["path"]).decode("utf-8", errors="replace") \
                    if isinstance(f["path"], (bytes, memoryview)) else f["path"]
                f["priority"] = FORMAT_PRIORITY.get(
                    (f["format"] or "").upper(), 9
                )

            # 推荐保留：priority 小 + bitrate 高
            ranked = sorted(
                files_dicts,
                key=lambda f: (f["priority"], -(f["bitrate"] or 0)),
            )
            keep = ranked[0]
            for f in files_dicts:
                f["recommended"] = f["id"] == keep["id"]
                f["disposition"] = "keep" if f["recommended"] else "drop"

            # 用文件名而非全 path 节省 UI 空间
            for f in files_dicts:
                f["filename"] = f["path"].rsplit("/", 1)[-1]
                f["dir"] = f["path"].rsplit("/", 1)[0]

            out.append({
                "mb_trackid": g.mb_trackid,
                "mb_releasegroupid": g.mb_releasegroupid,
                "count": int(g.cnt),
                "title": files_dicts[0].get("title") or "",
                "artist": files_dicts[0].get("artist") or "",
                "album": files_dicts[0].get("album") or "",
                "files": files_dicts,
            })
        return out


def _list_album_duplicates() -> list[dict[str, Any]]:
    """专辑级重复：同一张 release-group 的 items 散在 ≥2 个主文件夹里。

    "主文件夹" = items 路径里出现次数最多的 dirname。同一张专辑被扫两次
    （NAS FLAC + 备份 MP3，或 source/destination 没去重）会触发这条。

    每个候选文件夹返回：
      - 主目录、items 数、格式分布、平均码率、总字节数
      - 完整度 = 该文件夹内 items 数 / MB canonical 曲目数 (拉不到时返 None)
      - is_recommended：跨候选挑一份"无损优先 + 最完整"

    跟 _list_duplicate_groups() 互补：曲目级抓 FLAC+MP3 跨格式同曲，
    专辑级抓"整张被复制"。
    """
    import os  # noqa: PLC0415
    from collections import Counter  # noqa: PLC0415

    def _decode(p: object) -> str:
        if isinstance(p, (bytes, memoryview)):
            return bytes(p).decode("utf-8", errors="replace")
        return p or ""

    with get_engine().connect() as conn:
        # 先找有 ≥ 2 个 distinct dir 的 release-group —— 这是粗筛，再细判
        rgs = conn.execute(
            text(
                """SELECT mb_releasegroupid, COUNT(*) AS n
                   FROM beets.items
                   WHERE mb_releasegroupid != ''
                   GROUP BY mb_releasegroupid
                   HAVING n > 1
                   ORDER BY n DESC"""
            )
        ).all()

        out: list[dict[str, Any]] = []
        for rg in rgs:
            mbid = rg.mb_releasegroupid
            rows = conn.execute(
                text(
                    """SELECT id, path, format, bitrate, length, title
                       FROM beets.items
                       WHERE mb_releasegroupid = :rg"""
                ),
                {"rg": mbid},
            ).all()

            # 按主文件夹分组 items
            by_dir: dict[str, list[Any]] = {}
            for r in rows:
                p = _decode(r.path)
                if not p:
                    continue
                d = os.path.dirname(p)
                by_dir.setdefault(d, []).append(r)
            if len(by_dir) < 2:
                continue  # 只有一个目录，曲目级 dedup 才管它

            # MB canonical 曲目数（用 mb_release_group.tracks_json 拉，没就 None）
            head = conn.execute(
                text(
                    """SELECT title, artist_mbid, tracks_json
                       FROM mb_release_group WHERE mbid=:m"""
                ),
                {"m": mbid},
            ).first()
            mb_total: int | None = None
            title = ""
            artist_mbid = ""
            if head:
                title = head.title or ""
                artist_mbid = head.artist_mbid or ""
                if head.tracks_json:
                    try:
                        mb_total = len(json.loads(head.tracks_json))
                    except (TypeError, ValueError):
                        mb_total = None

            artist_name = ""
            if artist_mbid:
                a = conn.execute(
                    text("SELECT name FROM mb_artist WHERE mbid=:m"),
                    {"m": artist_mbid},
                ).first()
                if a:
                    artist_name = a.name or ""

            candidates: list[dict[str, Any]] = []
            for d, its in by_dir.items():
                fmts = Counter((it.format or "").upper() for it in its)
                avg_bitrate = (
                    sum(int(it.bitrate or 0) for it in its) // max(1, len(its))
                ) // 1000
                # 估总大小：用 length * bitrate 近似，免做 os.stat（NAS 上慢）
                est_bytes = sum(
                    int((it.length or 0) * (it.bitrate or 0) / 8) for it in its
                )
                # 找该候选最"贵"格式作排序键
                best_prio = min(
                    (FORMAT_PRIORITY.get(f, 9) for f in fmts), default=9
                )
                completeness = (
                    (len(its) / mb_total) if mb_total else None
                )
                candidates.append({
                    "dir": d,
                    "item_count": len(its),
                    "formats": dict(fmts),
                    "avg_bitrate_kbps": int(avg_bitrate),
                    "est_size_bytes": int(est_bytes),
                    "completeness": completeness,
                    "_prio": best_prio,
                    "item_ids": [int(it.id) for it in its],
                })

            # 排序：无损优先 + 完整度高 + items 多
            ranked = sorted(
                candidates,
                key=lambda c: (
                    c["_prio"],
                    -(c["completeness"] or 0),
                    -c["item_count"],
                ),
            )
            for i, c in enumerate(ranked):
                c["recommended"] = i == 0
                c.pop("_prio", None)

            out.append({
                "mb_releasegroupid": mbid,
                "title": title,
                "artist": artist_name,
                "mb_total_tracks": mb_total,
                "folder_count": len(ranked),
                "candidates": ranked,
            })
        return out


PLAYABLE_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".aiff", ".alac"}

# 浏览器 HTML5 <audio> 普遍能直放的格式 —— 这些走原码不转码省一笔 CPU + 转码缓存。
# FLAC / WAV / AIFF 虽然现代浏览器也支持，但体积巨大（FLAC 单曲 30+MB / WAV 更大），
# 走 AAC 192k 转码更经济，所以不放这里。
BROWSER_NATIVE_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus"}
MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".alac": "audio/mp4",
}


@router.get("/items")
async def list_items(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    artist_mbid: str | None = None,
    release_group_mbid: str | None = None,
    has_audio: bool = True,
    q: str | None = None,
) -> dict:
    """列出 items（iOS / 浏览用）.

    has_audio=True: 只返回 AVPlayer 能直播的格式（filter .ape 等）
    """
    where: list[str] = []
    params: dict[str, Any] = {}
    if artist_mbid:
        where.append("(mb_albumartistid = :am OR mb_artistid = :am)")
        params["am"] = artist_mbid
    if release_group_mbid:
        where.append("mb_releasegroupid = :rg")
        params["rg"] = release_group_mbid
    if q:
        where.append("(title LIKE :q OR artist LIKE :q OR album LIKE :q)")
        params["q"] = f"%{q}%"

    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""SELECT id, path, title, artist, album, length, format,
                     bitrate, mb_trackid, mb_releasegroupid
              FROM beets.items
              {where_clause}
              ORDER BY id DESC
              LIMIT :limit OFFSET :offset"""
    params["limit"] = limit
    params["offset"] = offset

    out: list[dict[str, Any]] = []
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(text(sql), params).all()
    except Exception:
        return {"count": 0, "items": []}

    for r in rows:
        d = dict(r._mapping)
        if has_audio:
            ext = PPath(os.fsdecode(d["path"])).suffix.lower() if d.get("path") else ""
            if ext not in PLAYABLE_EXTS:
                continue
        # 路径不返回客户端（信息泄露），但 stream endpoint 用 id 即可
        d.pop("path", None)
        d["bitrate_kbps"] = int(d["bitrate"] / 1000) if d.get("bitrate") else 0
        d.pop("bitrate", None)
        d["length"] = float(d["length"]) if d.get("length") else 0.0
        out.append(d)
    return {"count": len(out), "items": out}


TRANSCODE_NEEDED_EXTS = {".ape", ".wv", ".tak", ".tta"}

# 客户端可请求的合法封面尺寸（CAA 服务端缩略图档位）
COVER_SIZES = {250, 500, 1200}


def _ensure_cover_pref_table() -> None:
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """CREATE TABLE IF NOT EXISTS release_group_cover_pref (
                    mbid     TEXT PRIMARY KEY,
                    image_id TEXT NOT NULL,
                    set_at   INTEGER NOT NULL
                )"""
            )
        )


def _cover_pref(mbid: str) -> str | None:
    _ensure_cover_pref_table()
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT image_id FROM release_group_cover_pref WHERE mbid=:m"),
            {"m": mbid},
        ).first()
        return row.image_id if row else None


@router.get("/covers/release-group/{mbid}/list")
async def cover_list(mbid: str) -> dict:
    """列出三类候选封面：自己上传的 / 本地目录里的 / CAA 上的."""
    import base64  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    pref = _cover_pref(mbid)
    out: list[dict] = []

    # 1. 用户上传的自定义封面
    custom = _custom_cover_path(mbid)
    if custom is not None:
        out.append({
            "image_id": "custom",
            "source": "uploaded",
            "types": ["Custom"],
            "front": True,
            "back": False,
            "comment": "你上传的",
            "selected": pref == "custom",
            "thumb_url": f"/api/v1/covers/release-group/{mbid}/250",
        })

    # 2. 本地音乐目录里的图（用户自己放的、或者 ripping 软件留下的）
    for local in _scan_local_covers(mbid):
        encoded = base64.urlsafe_b64encode(str(local).encode()).decode().rstrip("=")
        img_id = f"local:{encoded}"
        out.append({
            "image_id": img_id,
            "source": "local",
            "types": ["Local"],
            "front": True,
            "back": False,
            "comment": local.name,
            "selected": pref == img_id,
            "thumb_url": f"/api/v1/covers/release-group/{mbid}/local/{encoded}",
        })

    # 3. CAA 上的候选
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(f"https://coverartarchive.org/release-group/{mbid}")
    except httpx.RequestError:
        resp = None
    if resp is not None and resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        for img in data.get("images") or []:
            img_id = str(img.get("id") or "")
            if not img_id:
                continue
            out.append({
                "image_id": img_id,
                "source": "caa",
                "types": img.get("types", []),
                "front": bool(img.get("front")),
                "back": bool(img.get("back")),
                "comment": img.get("comment", ""),
                "selected": img_id == pref,
                "thumb_url": f"/api/v1/covers/release-group/{mbid}/image/{img_id}/250",
            })

    return {"release_group_mbid": mbid, "images": out, "selected": pref}


def _scan_local_covers(mbid: str) -> list[PPath]:
    """扫该 release-group 所有 album 目录里的图片（排除我们自动写的 cover.jpg 和 artist.jpg）."""
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    exclude_names = {"cover.jpg", "artist.jpg"}

    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT path FROM beets.items WHERE mb_releasegroupid=:m"),
            {"m": mbid},
        ).all()

    seen_dirs: set[PPath] = set()
    found: list[PPath] = []
    for r in rows:
        raw = r.path
        ps = bytes(raw).decode("utf-8", errors="replace") \
            if isinstance(raw, (bytes, memoryview)) else str(raw)
        parent = PPath(ps).parent
        if parent in seen_dirs or not parent.exists():
            continue
        seen_dirs.add(parent)
        try:
            for child in sorted(parent.iterdir()):
                if (
                    child.is_file()
                    and child.suffix.lower() in image_exts
                    and child.name.lower() not in exclude_names
                ):
                    found.append(child)
        except OSError:
            continue
    return found


@router.get("/covers/release-group/{mbid}/local/{path_b64}")
async def cover_local(mbid: str, path_b64: str):
    """serve 本地音乐目录里的某张图（之前 /list 返回的 local:xxx）."""
    import base64  # noqa: PLC0415

    try:
        decoded = base64.urlsafe_b64decode(path_b64 + "==").decode()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, detail=f"bad path: {e}") from e
    p = PPath(decoded)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, detail="local image not found")

    # 安全闸：只允许返回 music_root 子目录里的文件
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    try:
        p.resolve().relative_to(s.music_root.resolve())
    except ValueError:
        raise HTTPException(403, detail="path outside music root")  # noqa: B904

    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp",
            ".gif": "image/gif"}.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=mime)


@router.post("/covers/release-group/{mbid}/upload")
async def upload_custom_cover(mbid: str, file: UploadFile = File(...)) -> dict:
    """用户从手机相册上传一张图当作这张专辑的封面.

    存到 `data/covers/custom_{mbid}.<ext>`，preference 记 image_id="custom"。
    后续 cover 请求会优先返回这张。
    """
    import time as _t  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    s.covers_dir.mkdir(parents=True, exist_ok=True)

    # 简单根据 content-type 推 ext；不识别就当 jpg
    ct = (file.content_type or "").lower()
    ext = "jpg"
    if "png" in ct: ext = "png"
    elif "webp" in ct: ext = "webp"
    elif "heic" in ct: ext = "heic"

    # 先清掉同 mbid 其他扩展的旧 custom 文件
    for old_ext in ("jpg", "png", "webp", "heic"):
        old = s.covers_dir / f"custom_{mbid}.{old_ext}"
        if old.exists():
            try: old.unlink()
            except OSError: pass

    custom_path = s.covers_dir / f"custom_{mbid}.{ext}"
    data = await file.read()
    if not data:
        raise HTTPException(400, detail="empty upload")
    custom_path.write_bytes(data)

    # 同时清掉旧的 size-级别缓存
    for size in (250, 500, 1200):
        for kind in ("front",):  # 旧默认缓存
            _ = kind
        old = s.covers_dir / f"rg_{mbid}_{size}.jpg"
        if old.exists():
            try: old.unlink()
            except OSError: pass

    # preference = "custom"
    _ensure_cover_pref_table()
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO release_group_cover_pref (mbid, image_id, set_at)
                   VALUES (:m, 'custom', :n)
                   ON CONFLICT(mbid) DO UPDATE SET image_id='custom', set_at=excluded.set_at"""
            ),
            {"m": mbid, "n": int(_t.time())},
        )

    # 写一份 cover.jpg 到所有 disc 目录
    if s.allow_file_writes:
        _write_cover_into_album_dirs(mbid, data)

    return {"ok": True, "release_group_mbid": mbid, "image_id": "custom",
            "bytes": len(data)}


def _custom_cover_path(mbid: str) -> "PPath | None":
    """如果用户上传过自定义封面就返回它，否则 None."""
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    for ext in ("jpg", "png", "webp", "heic"):
        p = s.covers_dir / f"custom_{mbid}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


@router.post("/covers/release-group/{mbid}/preference")
async def set_cover_pref(mbid: str, payload: dict) -> dict:
    """用户选了一张封面，记住 + 清掉旧的缓存让下次 /covers/.. 重新抓."""
    image_id = (payload.get("image_id") or "").strip()
    if not image_id:
        raise HTTPException(400, detail="missing image_id")
    _ensure_cover_pref_table()
    import time as _t  # noqa: PLC0415

    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO release_group_cover_pref (mbid, image_id, set_at)
                   VALUES (:m, :i, :n)
                   ON CONFLICT(mbid) DO UPDATE SET image_id=excluded.image_id,
                                                  set_at=excluded.set_at"""
            ),
            {"m": mbid, "i": image_id, "n": int(_t.time())},
        )

    # 清掉旧的 size-级别缓存，下次请求重新抓新 image_id
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    for size in (250, 500, 1200):
        old = s.covers_dir / f"rg_{mbid}_{size}.jpg"
        if old.exists():
            try:
                old.unlink()
            except OSError:
                pass

    # 顺手也清掉同步写到 album 目录里的 cover.jpg（让下一次 /covers/.. 重写）
    if s.allow_file_writes:
        with get_engine().connect() as conn:
            rows = conn.execute(
                text("SELECT DISTINCT path FROM beets.items WHERE mb_releasegroupid=:m"),
                {"m": mbid},
            ).all()
        for r in rows:
            raw = r.path
            ps = bytes(raw).decode("utf-8", errors="replace") \
                if isinstance(raw, (bytes, memoryview)) else str(raw)
            target = PPath(ps).parent / "cover.jpg"
            if target.exists():
                try:
                    target.unlink()
                except OSError:
                    pass

    return {"ok": True, "release_group_mbid": mbid, "image_id": image_id}


async def _caa_image_url(mbid: str, image_id: str, size: int) -> str | None:
    """从 CAA listing JSON 查指定 image_id 在指定 size 的实际 archive.org URL.

    CAA `/release-group/{mbid}/{image_id}-{size}` 不被支持（只有 release 级别可以这样取），
    所以必须先 GET listing JSON。
    """
    import httpx  # noqa: PLC0415

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(f"https://coverartarchive.org/release-group/{mbid}")
    except httpx.RequestError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    for img in data.get("images") or []:
        if str(img.get("id") or "") == image_id:
            thumbs = img.get("thumbnails") or {}
            return (
                thumbs.get(str(size))
                or thumbs.get({250: "small", 500: "large", 1200: "1200"}[size])
                or img.get("image")
            )
    return None


@router.get("/covers/release-group/{mbid}/image/{image_id}/{size}")
async def cover_by_image_id(mbid: str, image_id: str, size: int):
    """按 CAA image_id 取特定一张图，自己缓存."""
    import httpx  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415

    if size not in COVER_SIZES:
        raise HTTPException(400, detail="size must be 250 / 500 / 1200")

    s = get_settings()
    s.covers_dir.mkdir(parents=True, exist_ok=True)
    cache_path = s.covers_dir / f"rg_{mbid}_img_{image_id}_{size}.jpg"

    if cache_path.exists() and cache_path.stat().st_size > 0:
        cache_path.touch()
        return FileResponse(cache_path, media_type="image/jpeg")

    upstream = await _caa_image_url(mbid, image_id, size)
    if upstream is None:
        raise HTTPException(404, detail="image not in CAA listing")

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(upstream)
    except httpx.RequestError as e:
        raise HTTPException(502, detail=f"caa: {e}") from e

    if resp.status_code != 200:
        raise HTTPException(502, detail=f"caa HTTP {resp.status_code}")

    cache_path.write_bytes(resp.content)
    return FileResponse(cache_path, media_type="image/jpeg")


@router.get("/covers/release-group/{mbid}/{size}")
async def cover_release_group(mbid: str, size: int):
    """代理 + 本地磁盘缓存 release-group 封面.

    优点：客户端不直连 archive.org（隐私 + 单一来源），服务器一次抓多次发，
    本地有缓存就秒发。命中 → touch mtime 当作 last-hit。
    """
    import logging  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415

    log = logging.getLogger(__name__)

    if size not in COVER_SIZES:
        raise HTTPException(400, detail="size must be 250 / 500 / 1200")

    s = get_settings()
    s.covers_dir.mkdir(parents=True, exist_ok=True)

    # 用户从手机上传的自定义封面优先（含 localdir 合成 mbid + is_local 都吃这一条）
    pref = _cover_pref(mbid)
    if pref == "custom":
        custom = _custom_cover_path(mbid)
        if custom is not None:
            ext = custom.suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "webp": "image/webp",
                    "heic": "image/heic"}.get(ext, "application/octet-stream")
            return FileResponse(custom, media_type=mime)

    # 没自定义封面 → localdir 合成 mbid 直接 SVG fallback（用目录名做 label）
    if mbid.startswith(_LOCAL_DIR_PREFIX):
        import os  # noqa: PLC0415
        dir_path = _decode_local_album_mbid(mbid) or ""
        title = os.path.basename(dir_path) or "?"
        svg = _generate_fallback_cover(mbid, size, explicit_title=title)
        return Response(
            content=svg, media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # is_local 专辑同样跳过 CAA，用存的标题生成 SVG
    with get_engine().connect() as _c:
        _row = _c.execute(
            text("SELECT is_local, title FROM mb_release_group WHERE mbid=:m"),
            {"m": mbid},
        ).first()
        if _row and (_row.is_local or 0):
            svg = _generate_fallback_cover(
                mbid, size, explicit_title=(_row.title or "?")
            )
            return Response(
                content=svg, media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=86400"},
            )

    cache_path = s.covers_dir / f"rg_{mbid}_{size}.jpg"

    if cache_path.exists() and cache_path.stat().st_size > 0:
        cache_path.touch()
        return FileResponse(cache_path, media_type="image/jpeg")

    if pref and pref != "custom" and not pref.startswith("local:"):
        # 用户选了 CAA 上某个 image_id —— 查 listing 拿 thumbnail URL
        url = await _caa_image_url(mbid, pref, size)
        upstream = url or f"https://coverartarchive.org/release-group/{mbid}/front-{size}"
    elif pref and pref.startswith("local:"):
        # 用户选了本地某张图，直接返回文件
        import base64  # noqa: PLC0415

        try:
            local_path = PPath(base64.urlsafe_b64decode(pref[len("local:"):] + "==").decode())
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, detail=f"bad local pref: {e}") from e
        if local_path.exists():
            return FileResponse(local_path)
        upstream = f"https://coverartarchive.org/release-group/{mbid}/front-{size}"
    else:
        upstream = f"https://coverartarchive.org/release-group/{mbid}/front-{size}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(upstream)
    except httpx.RequestError as e:
        raise HTTPException(502, detail=f"upstream: {e}") from e

    if resp.status_code == 404:
        # MB / CAA 上没图 —— 生成一张 SVG fallback（首字母 + 紫色调）
        svg = _generate_fallback_cover(mbid, size)
        return Response(
            content=svg, media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    if resp.status_code != 200:
        raise HTTPException(502, detail=f"upstream HTTP {resp.status_code}")

    cache_path.write_bytes(resp.content)
    log.info("cached cover rg=%s size=%d (%d KB)",
             mbid, size, len(resp.content) // 1024)

    # 500 尺寸的封面同时写一份 cover.jpg 到对应的专辑目录
    if size == 500 and s.allow_file_writes:
        _write_cover_into_album_dirs(mbid, resp.content)

    return FileResponse(cache_path, media_type="image/jpeg")


def _generate_fallback_cover(mbid: str, size: int, *, explicit_title: str = "") -> bytes:
    """没真封面时给 release-group 生成一张 SVG："首字母 + 渐变背景"。
    色相基于 mbid hash 稳定；标题优先 explicit_title (localdir 兜底用)，
    其次从 mb_release_group 拿；都没有就用 ?
    """
    import hashlib  # noqa: PLC0415

    title = (explicit_title or "").strip()
    if not title:
        with get_engine().connect() as conn:
            row = conn.execute(
                text("SELECT title FROM mb_release_group WHERE mbid=:m"),
                {"m": mbid},
            ).first()
            if row:
                title = (row.title or "").strip()

    label = (title[:2] if title else "?").upper()

    # 色相按 mbid 稳定算
    h = int(hashlib.md5(mbid.encode()).hexdigest()[:4], 16) % 360
    bg1 = f"hsl({h}, 60%, 40%)"
    bg2 = f"hsl({(h + 35) % 360}, 70%, 25%)"
    fg = "rgba(255,255,255,0.92)"

    font_size = int(size * 0.45)

    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">
  <defs>
    <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="{bg1}"/>
      <stop offset="100%" stop-color="{bg2}"/>
    </linearGradient>
  </defs>
  <rect width="{size}" height="{size}" fill="url(#g)"/>
  <text x="50%" y="50%" font-family="-apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif"
        font-size="{font_size}" font-weight="800" letter-spacing="-2"
        text-anchor="middle" dominant-baseline="central"
        fill="{fg}">{_escape_xml(label)}</text>
</svg>'''
    return svg.encode("utf-8")


def _escape_xml(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _write_cover_into_album_dirs(mbid: str, data: bytes) -> None:
    """把 cover.jpg 写到该 release-group 所有曲目对应的目录里（每盘一份）."""
    import logging  # noqa: PLC0415

    log = logging.getLogger(__name__)

    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT path FROM beets.items WHERE mb_releasegroupid = :rg"),
            {"rg": mbid},
        ).all()

    dirs: set[PPath] = set()
    for r in rows:
        raw = r.path
        if isinstance(raw, (bytes, memoryview)):
            path_str = bytes(raw).decode("utf-8", errors="replace")
        else:
            path_str = str(raw)
        dirs.add(PPath(path_str).parent)

    for d in dirs:
        if not d.exists():
            continue
        target = d / "cover.jpg"
        if target.exists():
            continue   # 不覆盖用户既有的封面
        try:
            target.write_bytes(data)
            log.info("wrote cover.jpg → %s", d)
        except OSError as e:
            log.warning("failed to write cover.jpg → %s: %s", d, e)


def _range_response(data: bytes, mime: str, range_header: str | None) -> Response:
    """对内存 bytes 实现最小够用的 HTTP Range；AVPlayer seek 走它."""
    total = len(data)
    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    if not range_header or not range_header.startswith("bytes="):
        return Response(
            content=data,
            media_type=mime,
            headers={**base_headers, "Content-Length": str(total)},
        )
    spec = range_header[len("bytes="):].split("-", 1)
    try:
        start = int(spec[0]) if spec[0] else 0
        end = int(spec[1]) if len(spec) > 1 and spec[1] else total - 1
    except ValueError:
        return Response(status_code=416, headers=base_headers)
    start = max(0, start)
    end = min(end, total - 1)
    if start > end:
        return Response(status_code=416, headers=base_headers)
    chunk = data[start:end + 1]
    return Response(
        content=chunk,
        status_code=206,
        media_type=mime,
        headers={
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Content-Length": str(len(chunk)),
        },
    )


@router.delete("/local-albums/{mbid}")
async def delete_local_album(mbid: str) -> dict:
    """删一张 is_local=1 的本地策划专辑。

    流程：
      1. 检查是 is_local=1（拒绝删 MB 缓存的真专辑）
      2. 把还绑在这张 rg 上的 items 全部清掉 mb_releasegroupid + mb_trackid，
         它们退回 localdir 兜底视图，文件本身不删
      3. DELETE mb_release_group 行

    用户场景：创建错了想撤回。文件还在硬盘上，目录视图也还在。
    """
    if not mbid:
        raise HTTPException(400, detail="mbid required")

    from app import beets_bridge  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT mbid, COALESCE(is_local, 0) AS is_local FROM mb_release_group WHERE mbid=:m"),
            {"m": mbid},
        ).first()
        if not row:
            raise HTTPException(404, detail="release-group not found")
        if not (row.is_local or 0):
            raise HTTPException(
                400, detail="refuse to delete a non-local release-group (only is_local=1 can be deleted)"
            )
        # 拉绑在这张上的所有 items，准备清
        bound = conn.execute(
            text("SELECT id FROM beets.items WHERE mb_releasegroupid = :m"),
            {"m": mbid},
        ).all()

    # 通过 beets API 清 items 元数据（直接 SQL UPDATE 也行，但走 bridge 跟 bind 路径一致）
    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    unbound = 0
    for r in bound:
        if beets_bridge.set_item_meta(
            lib, int(r.id), track_mbid="", releasegroup_mbid=""
        ):
            unbound += 1

    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM mb_release_group WHERE mbid=:m"), {"m": mbid})

    return {"ok": True, "deleted": mbid, "unbound_items": unbound}


@router.post("/local-albums")
async def create_local_album(payload: dict) -> dict:
    """新建一张本地策划专辑 (MB 上没收录的) —— 写入 mb_release_group 表，is_local=1。

    payload:
      - title: str (必填)
      - artist_name: str (artist_mbid 没填时显示名)
      - artist_mbid: str (可选，知道艺人 MB id 就填，能跟艺人页关联上)
      - primary_type: 'Album' | 'Single' | 'EP' | ... (可选)
      - first_release_date: 'YYYY' | 'YYYY-MM-DD' (可选)
      - tracks: [{ title: str, length_s?: float, position?: int, disc?: int }]
        position 不传时按数组顺序 1..N

    tracks 里每首生成一个 uuid4 作为 recording_mbid，跟 MB 真 uuid 同格式，
    items 后续通过 drag-bind 把自己的 mb_trackid 设成这些 synth id。
    """
    import json  # noqa: PLC0415
    import uuid  # noqa: PLC0415
    import time  # noqa: PLC0415

    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, detail="title required")
    artist_name = (payload.get("artist_name") or "").strip()
    artist_mbid = (payload.get("artist_mbid") or "").strip()
    primary_type = (payload.get("primary_type") or "Album").strip()
    first_release_date = (payload.get("first_release_date") or "").strip()
    tracks_in = payload.get("tracks") or []
    if not isinstance(tracks_in, list) or not tracks_in:
        raise HTTPException(400, detail="tracks required (non-empty list)")

    tracks_out: list[dict] = []
    for i, t in enumerate(tracks_in):
        if not isinstance(t, dict):
            continue
        ttitle = (t.get("title") or "").strip()
        if not ttitle:
            continue
        tracks_out.append({
            "disc": int(t.get("disc", 1) or 1),
            "position": int(t.get("position") or (i + 1)),
            "recording_mbid": str(uuid.uuid4()),
            "title": ttitle,
            "length_s": float(t.get("length_s") or 0),
        })
    if not tracks_out:
        raise HTTPException(400, detail="tracks must contain at least one title")

    rg_mbid = str(uuid.uuid4())
    now = int(time.time())

    # 没指定 artist_mbid 时给一个稳定的"未知本地艺人"sentinel，并确保 mb_artist 表里有
    UNKNOWN_LOCAL_ARTIST_MBID = "00000000-0000-0000-0000-000000000000"

    with get_engine().begin() as conn:
        # 兜底：旧库可能还没有 is_local/artist_name 列
        cols = {r[1] for r in conn.execute(
            text("PRAGMA table_info(mb_release_group)")
        ).all()}
        if "is_local" not in cols:
            conn.execute(text(
                "ALTER TABLE mb_release_group ADD COLUMN is_local INTEGER NOT NULL DEFAULT 0"
            ))
        if "artist_name" not in cols:
            conn.execute(text(
                "ALTER TABLE mb_release_group ADD COLUMN artist_name TEXT NOT NULL DEFAULT ''"
            ))
        if "tracks_json" not in cols:
            conn.execute(text(
                "ALTER TABLE mb_release_group ADD COLUMN tracks_json TEXT"
            ))

        # 满足 FK：artist_mbid 必须在 mb_artist 里有一行
        effective_artist_mbid = artist_mbid or UNKNOWN_LOCAL_ARTIST_MBID
        exists = conn.execute(
            text("SELECT 1 FROM mb_artist WHERE mbid=:m"),
            {"m": effective_artist_mbid},
        ).first()
        if not exists:
            conn.execute(
                text(
                    """INSERT INTO mb_artist
                       (mbid, name, sort_name, fetched_at, stale_after)
                       VALUES (:m, :n, :n, :now, :stale)"""
                ),
                {
                    "m": effective_artist_mbid,
                    "n": artist_name or "(unknown)",
                    "now": now,
                    "stale": now + 86400 * 365 * 10,  # 这种 sentinel/local 艺人不去刷
                },
            )

        conn.execute(
            text(
                """INSERT INTO mb_release_group
                   (mbid, title, primary_type, first_release_date,
                    artist_mbid, artist_name, tracks_json, is_local)
                   VALUES (:mbid, :title, :pt, :frd, :am, :an, :tj, 1)"""
            ),
            {
                "mbid": rg_mbid,
                "title": title,
                "pt": primary_type,
                "frd": first_release_date,
                "am": effective_artist_mbid,
                "an": artist_name,
                "tj": json.dumps(tracks_out, ensure_ascii=False),
            },
        )

    return {
        "mbid": rg_mbid,
        "title": title,
        "artist_mbid": artist_mbid,
        "artist": artist_name,
        "primary_type": primary_type,
        "first_release_date": first_release_date,
        "tracks": tracks_out,
        "is_local": True,
    }


@router.post("/local-albums/{local_mbid}/bind-to-mb")
async def bind_local_album_to_mb(local_mbid: str, payload: dict) -> dict:
    """把一个 localdir-... 合成专辑里所有 items 的 mb_releasegroupid 一次性
    设成真正的 MB release-group。

    body: { "rg_mbid": "<uuid>" } 也接受贴 MB URL，前端清洗后送 uuid 过来。

    顺手做两件事：
      - 确保 mb_release_group 缓存表里有这张 rg（调 release_group_tracks
        触发拉 MB + 缓存 tracks_json）
      - 把 items.mb_artistid 也写上（如果 rg 知道 artist_mbid 且 items 那
        一列是空）

    完成后这个 localdir 合成专辑就消失了（items 都不再属于"空 mb_releasegroupid"），
    用户访问 /release-groups/{rg_mbid}/playable 走正常路径，单曲还得用拖拽
    绑 mb_trackid。
    """
    import re  # noqa: PLC0415

    if not local_mbid.startswith(_LOCAL_DIR_PREFIX):
        raise HTTPException(400, detail="not a local album mbid")
    dir_path = _decode_local_album_mbid(local_mbid)
    if not dir_path:
        raise HTTPException(400, detail="bad local mbid")

    rg_raw = (payload.get("rg_mbid") or "").strip()
    m = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        rg_raw, re.IGNORECASE,
    )
    if not m:
        raise HTTPException(400, detail="rg_mbid required (UUID or MB URL)")
    rg_mbid = m.group(0).lower()

    # 先把 mb_release_group 缓存拉上（顺便能拿到 artist_mbid）
    try:
        await release_group_tracks(rg_mbid)
    except HTTPException:
        pass  # 拉不到也不阻塞 binding；前端进去就是空 tracks + orphan

    with get_engine().connect() as conn:
        head = conn.execute(
            text("SELECT artist_mbid FROM mb_release_group WHERE mbid=:m"),
            {"m": rg_mbid},
        ).first()
        new_artist_mbid = (head.artist_mbid or "") if head else ""

    # 把目录下所有 items 的 mb_releasegroupid 设掉
    from app import beets_bridge  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """SELECT id, mb_artistid FROM beets.items
                   WHERE CAST(path AS TEXT) LIKE :p"""
            ),
            {"p": dir_path.rstrip("/") + "/%"},
        ).all()

    updated = 0
    for r in rows:
        kwargs = {"releasegroup_mbid": rg_mbid}
        if new_artist_mbid and not (r.mb_artistid or ""):
            kwargs["artist_mbid"] = new_artist_mbid
        if beets_bridge.set_item_meta(lib, int(r.id), **kwargs):
            updated += 1

    # 心愿单 phase 2: 目录批量绑到 MB 真专辑后, 看看这张是不是心愿单上的
    _fulfill_after_bind()
    return {"ok": True, "rg_mbid": rg_mbid, "updated_items": updated}


def _fulfill_after_bind() -> int:
    """绑定流程之后顺手 fulfill 心愿单 —— 把 wishlist 跟 items 表对一遍，
    items 已经有的对应专辑 fulfilled_at 标 now。返回新 fulfill 几条 (调用方
    可以日志)。失败兜底返 0 不抛，免得影响主流程。"""
    try:
        from app.api.wishlist import _fulfill_matching_wishlist  # noqa: PLC0415
        return _fulfill_matching_wishlist()
    except Exception:
        return 0


@router.post("/items/{item_id}/bind")
async def bind_item(item_id: int, payload: dict) -> dict:
    """把一首本地 item 绑到指定 MB recording + release-group。

    Web 端把 orphan 拖到 MB 曲目行用这个；之前没有从 web 改 mb_trackid 的入口。
    payload: { "recording_mbid": str, "release_group_mbid": str }
    """
    rec = (payload.get("recording_mbid") or "").strip()
    rg = (payload.get("release_group_mbid") or "").strip()
    if not rec or not rg:
        raise HTTPException(400, detail="recording_mbid + release_group_mbid required")

    from app import beets_bridge  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    ok = beets_bridge.set_item_meta(
        lib, item_id, track_mbid=rec, releasegroup_mbid=rg
    )
    if not ok:
        raise HTTPException(404, detail="item not found")
    # 心愿单 phase 2: 这次绑定可能让某张心愿单专辑命中本地，把它打勾
    _fulfill_after_bind()
    return {
        "ok": True,
        "item_id": item_id,
        "recording_mbid": rec,
        "release_group_mbid": rg,
    }


@router.post("/release-groups/{mbid}/clear-items")
async def clear_release_group_items(mbid: str) -> dict:
    """把所有绑到这张 release-group 的 items 一键摘下来 (mb_trackid + mb_releasegroupid 清空)。

    场景：fingerprint 把同目录一堆文件全错认到一张幻影 MB 专辑上 (典型：
    女子十二的'魅力音乐会'识别成另一张专辑)。逐首 ✕ 解绑太累，这个直接清掉。

    不删 rg 行（is_local 才允许 DELETE）。items 摘干净后这张 MB album 自然
    从艺人页消失 (owned_albums 的 EXISTS 没人命中了)。文件不动。
    """
    if not mbid:
        raise HTTPException(400, detail="mbid required")

    from app import beets_bridge  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT id FROM beets.items WHERE mb_releasegroupid = :m"),
            {"m": mbid},
        ).all()
    if not rows:
        return {"ok": True, "mbid": mbid, "unbound_items": 0}

    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    unbound = 0
    for r in rows:
        if beets_bridge.set_item_meta(
            lib, int(r.id), track_mbid="", releasegroup_mbid=""
        ):
            unbound += 1
    return {"ok": True, "mbid": mbid, "unbound_items": unbound}


@router.post("/items/{item_id}/unbind")
async def unbind_item(item_id: int) -> dict:
    """清掉 item 的 mb_trackid + mb_releasegroupid。

    场景：fingerprint 把一首歌错认到某张专辑了，幻影"1 首歌专辑"挂在艺人页。
    用户解绑后 item 回到本地兜底视图，幻影专辑自然消失 (没 item 命中了)。
    文件本身不动。
    """
    from app import beets_bridge  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    ok = beets_bridge.set_item_meta(
        lib, item_id, track_mbid="", releasegroup_mbid=""
    )
    if not ok:
        raise HTTPException(404, detail="item not found")
    return {"ok": True, "item_id": item_id}


@router.post("/items/{item_id}/prewarm")
async def prewarm_item(
    item_id: int,
    fmt: str = Query("", description="aac → AAC 转码；空 → APE 等才转 FLAC"),
    q: int = Query(0, description="AAC 比特率 kbps"),
) -> dict:
    """触发后台内存转码。进专辑时给所有曲目调一遍，点曲就秒响应。"""
    import asyncio  # noqa: PLC0415

    from app import beets_bridge  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415
    from app.transcode.ffmpeg import (  # noqa: PLC0415
        get_or_transcode_aac,
        get_or_transcode_flac,
    )

    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    p = beets_bridge.get_item_path(lib, item_id)
    if p is None or not p.exists():
        raise HTTPException(404, detail="item not found")

    ext = p.suffix.lower()

    if fmt.lower() == "aac":
        bitrate = q if q > 0 else s.default_aac_bitrate
        asyncio.create_task(get_or_transcode_aac(item_id, p, bitrate))
        return {"queued": True, "target": f"aac_{bitrate}"}
    if ext in TRANSCODE_NEEDED_EXTS:
        asyncio.create_task(get_or_transcode_flac(item_id, p))
        return {"queued": True, "target": "flac"}
    return {"queued": False, "target": "passthrough"}


@router.get("/items/{item_id}/stream")
async def stream_item(
    item_id: int,
    request: Request,
    fmt: str = Query("", description="可选转码目标，例：aac"),
    q: int = Query(0, description="目标比特率 kbps；fmt=aac 时生效"),
):
    """流式播放音频。转码结果走 in-memory LRU，原码走 FileResponse。"""
    from app import beets_bridge  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415
    from app.transcode.ffmpeg import (  # noqa: PLC0415
        get_or_transcode_aac,
        get_or_transcode_flac,
    )

    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    p = beets_bridge.get_item_path(lib, item_id)
    if p is None or not p.exists():
        raise HTTPException(404, detail="item not found or file missing")

    ext = p.suffix.lower()
    range_header = request.headers.get("range")

    # 客户端要 AAC（蜂窝省流量）
    if fmt.lower() == "aac":
        bitrate = q if q > 0 else s.default_aac_bitrate
        try:
            data, mime = await get_or_transcode_aac(item_id, p, bitrate)
        except RuntimeError as e:
            raise HTTPException(500, detail=f"transcode failed: {e}") from e
        return _range_response(data, mime, range_header)

    # 中间层 fmt=auto：浏览器友好。MP3/M4A/AAC/OGG/Opus 原码直送，
    # FLAC/WAV/AIFF/ALAC + APE/WV/TAK/TTA 一律 AAC 192k。
    # iOS app 不要传 auto，它有自己的本地解码器。
    if fmt.lower() == "auto":
        if ext in BROWSER_NATIVE_EXTS:
            return FileResponse(p, media_type=MIME_BY_EXT.get(ext, "application/octet-stream"))
        bitrate = q if q > 0 else s.default_aac_bitrate
        try:
            data, mime = await get_or_transcode_aac(item_id, p, bitrate)
        except RuntimeError as e:
            raise HTTPException(500, detail=f"transcode failed: {e}") from e
        return _range_response(data, mime, range_header)

    # APE/WV/TAK → 自动 FLAC
    if ext in TRANSCODE_NEEDED_EXTS:
        try:
            data, mime = await get_or_transcode_flac(item_id, p)
        except RuntimeError as e:
            raise HTTPException(500, detail=f"transcode failed: {e}") from e
        return _range_response(data, mime, range_header)

    if ext not in PLAYABLE_EXTS:
        raise HTTPException(415, detail=f"unplayable format: {ext}")

    # 原码：直接走 FileResponse（带 Range，零拷贝）
    return FileResponse(p, media_type=MIME_BY_EXT.get(ext, "application/octet-stream"))


@router.get("/organize/preview")
async def organize_preview() -> dict:
    """规范化预览：所有可归档专辑的 src→dst 对，不动文件."""
    from app import beets_bridge, organize  # noqa: PLC0415
    from app.config import get_settings as _gs  # noqa: PLC0415

    s = _gs()
    if not s.beets_db.exists():
        return {"groups": [], "allow_file_writes": s.allow_file_writes}
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    groups = organize.compute_preview(lib)
    return {
        "groups": [g.to_dict() for g in groups],
        "allow_file_writes": s.allow_file_writes,
        "count": len(groups),
    }


@router.post("/organize/apply")
async def organize_apply(payload: dict) -> dict:
    """应用单组归档."""
    from app import beets_bridge, organize  # noqa: PLC0415
    from app.config import get_settings as _gs  # noqa: PLC0415

    src_dir = payload.get("src_dir")
    if not src_dir:
        return {"ok": False, "reason": "需要 src_dir"}
    s = _gs()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    return organize.apply_group(lib, src_dir)


@router.get("/duplicates")
async def list_duplicates() -> dict:
    try:
        groups = _list_duplicate_groups()
    except Exception:
        groups = []
    return {"count": len(groups), "groups": groups}


@router.get("/duplicates/albums")
async def list_album_duplicates() -> dict:
    """专辑级重复：同一张专辑分散在 ≥ 2 个主文件夹的情形。

    跟 /duplicates 互补：前者抓"同一首歌多份"，这个抓"整张被复制"。
    """
    try:
        albums = _list_album_duplicates()
    except Exception:
        albums = []
    return {"count": len(albums), "albums": albums}


@router.get("/artists/{mbid}")
async def artist_detail(mbid: str) -> dict:
    with get_engine().connect() as conn:
        artist_row = conn.execute(
            text("SELECT mbid, name, sort_name, country, disambiguation FROM mb_artist WHERE mbid=:m"),
            {"m": mbid},
        ).first()

        items_count = conn.execute(
            text(
                """SELECT COUNT(*) FROM beets.items
                   WHERE COALESCE(NULLIF(mb_albumartistid, ''), mb_artistid) = :m"""
            ),
            {"m": mbid},
        ).scalar() or 0

        rgs = conn.execute(
            text(
                """SELECT
                       rg.mbid, rg.title, rg.primary_type, rg.secondary_types,
                       rg.first_release_date, rg.cover_url,
                       (SELECT COUNT(*) FROM beets.items i
                        WHERE i.mb_releasegroupid = rg.mbid) AS owned_items
                   FROM mb_release_group rg
                   WHERE rg.artist_mbid = :m
                   ORDER BY
                     rg.first_release_date IS NULL OR rg.first_release_date = '',
                     rg.first_release_date"""
            ),
            {"m": mbid},
        ).all()

        # 给每个 release-group 算 CAA 封面 URL（直拼，不验存在性）
        def _caa_url(rg_mbid: str) -> str:
            return f"https://coverartarchive.org/release-group/{rg_mbid}/front-500"

    if not artist_row and items_count == 0 and not rgs:
        raise HTTPException(404, detail="artist not found")

    artist_payload: dict[str, Any]
    if artist_row:
        artist_payload = dict(artist_row._mapping)
    else:
        artist_payload = {"mbid": mbid, "name": None, "loading": True}

    return {
        "artist": artist_payload,
        "items_in_library": int(items_count),
        "release_groups": [
            {
                **dict(r._mapping),
                "owned": int(r.owned_items) > 0,
                "cover_url": r.cover_url or _caa_url(r.mbid),
            }
            for r in rgs
        ],
    }


@router.get("/artists/{mbid}/photo")
async def artist_photo(mbid: str):
    """从 TheAudioDB 拉艺人照片，缓存到 data/artist_photos/，
    同时写一份 artist.jpg 到该艺人目录（album dir 的父目录）.
    """
    import logging  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415

    log = logging.getLogger(__name__)
    s = get_settings()
    cache_dir = s.data_dir / "artist_photos"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{mbid}.jpg"

    if cache_path.exists() and cache_path.stat().st_size > 0:
        cache_path.touch()
        return FileResponse(cache_path, media_type="image/jpeg")

    # TheAudioDB —— test key "2" 够用；正式部署可换自己的 key
    api_url = f"https://www.theaudiodb.com/api/v1/json/2/artist-mb.php?i={mbid}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            meta_resp = await client.get(api_url)
        meta = meta_resp.json()
    except (httpx.RequestError, ValueError) as e:
        raise HTTPException(502, detail=f"audiodb meta: {e}") from e

    arts = meta.get("artists") or []
    photo_url = arts[0].get("strArtistThumb") if arts else None
    if not photo_url:
        raise HTTPException(404, detail="no artist photo")

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            photo_resp = await client.get(photo_url)
        photo_resp.raise_for_status()
    except httpx.RequestError as e:
        raise HTTPException(502, detail=f"photo dl: {e}") from e

    cache_path.write_bytes(photo_resp.content)
    log.info("cached artist photo mbid=%s (%d KB)", mbid, len(photo_resp.content) // 1024)

    if s.allow_file_writes:
        _write_artist_photo_into_dirs(mbid, photo_resp.content, s.music_root)

    return FileResponse(cache_path, media_type="image/jpeg")


def _write_artist_photo_into_dirs(mbid: str, data: bytes, music_root: PPath) -> None:
    """通过 beets 数据找该艺人所有曲目，取 track.parent.parent 当作 artist dir。"""
    import logging  # noqa: PLC0415

    log = logging.getLogger(__name__)

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """SELECT DISTINCT path FROM beets.items
                   WHERE COALESCE(NULLIF(mb_albumartistid,''), mb_artistid) = :m"""
            ),
            {"m": mbid},
        ).all()

    music_root = music_root.resolve()
    dirs: set[PPath] = set()
    for r in rows:
        raw = r.path
        path_str = bytes(raw).decode("utf-8", errors="replace") \
            if isinstance(raw, (bytes, memoryview)) else str(raw)
        artist_dir = PPath(path_str).resolve().parent.parent
        # 必须严格在 music_root 下，且不能就是 music_root（避免写到根目录）
        try:
            artist_dir.relative_to(music_root)
        except ValueError:
            continue
        if artist_dir == music_root:
            continue
        dirs.add(artist_dir)

    for d in dirs:
        if not d.exists():
            continue
        target = d / "artist.jpg"
        if target.exists():
            continue
        try:
            target.write_bytes(data)
            log.info("wrote artist.jpg → %s", d)
        except OSError as e:
            log.warning("failed to write artist.jpg → %s: %s", d, e)


@router.get("/release-groups/{mbid}/tracks")
async def release_group_tracks(mbid: str) -> dict:
    """拉 MB 上这张 release-group 第一个 release 的完整曲目表，
    用于"长按指认"sheet。结果缓存在 mb_release_group.tracks_json 列里。
    """
    import json  # noqa: PLC0415
    import logging  # noqa: PLC0415

    log = logging.getLogger(__name__)

    # 先确保 tracks_json 列存在（第一次跑会建）
    with get_engine().begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(mb_release_group)")).all()}
        if "tracks_json" not in cols:
            conn.execute(text("ALTER TABLE mb_release_group ADD COLUMN tracks_json TEXT"))

    # 再看缓存
    with get_engine().connect() as conn:
        row = conn.execute(
            text("""SELECT tracks_json FROM mb_release_group WHERE mbid=:m"""),
            {"m": mbid},
        ).first()
        cached = row.tracks_json if row else None

    if cached:
        try:
            return {"release_group_mbid": mbid, "tracks": json.loads(cached), "from_cache": True}
        except (TypeError, ValueError):
            pass   # fall through to refetch

    # 没缓存 → 走 MB
    import musicbrainzngs  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    musicbrainzngs.set_useragent("MusicTidy", "0.1", s.mb_user_agent)
    try:
        rg_result = await asyncio.to_thread(
            musicbrainzngs.get_release_group_by_id,
            mbid,
            includes=["releases"],
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, detail=f"MB release-group: {e}") from e

    releases = (rg_result.get("release-group") or {}).get("release-list", [])
    if not releases:
        raise HTTPException(404, detail="no releases under this release-group")

    # 选官方第一个 release（CD 优先）
    release = releases[0]
    try:
        rel_result = await asyncio.to_thread(
            musicbrainzngs.get_release_by_id,
            release["id"],
            includes=["recordings", "artist-credits"],
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, detail=f"MB release: {e}") from e

    rel = rel_result.get("release", {})
    tracks_out: list[dict] = []
    for medium in rel.get("medium-list", []) or []:
        disc_no = int(medium.get("position", 1))
        for tr in medium.get("track-list", []) or []:
            rec = tr.get("recording") or {}
            length_ms = int(rec.get("length") or tr.get("length") or 0)
            tracks_out.append({
                "disc": disc_no,
                "position": int(tr.get("position", 0) or 0),
                "recording_mbid": rec.get("id") or "",
                "title": rec.get("title") or tr.get("title", ""),
                "length_s": length_ms / 1000.0 if length_ms else 0.0,
            })

    # 缓存到 DB（需要先确保 tracks_json 列存在）
    with get_engine().begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(mb_release_group)")).all()}
        if "tracks_json" not in cols:
            conn.execute(text("ALTER TABLE mb_release_group ADD COLUMN tracks_json TEXT"))
        conn.execute(
            text("UPDATE mb_release_group SET tracks_json=:j WHERE mbid=:m"),
            {"j": json.dumps(tracks_out, ensure_ascii=False), "m": mbid},
        )
    log.info("cached %d tracks for rg=%s", len(tracks_out), mbid)
    return {"release_group_mbid": mbid, "tracks": tracks_out, "from_cache": False}


def _local_dir_playable(mbid: str) -> dict:
    """localdir-<base64> 合成 mbid 的视图：把同目录的 items 全部当 orphan 返回。

    跟正经 MB 专辑视图共用一个 endpoint，前端不用判断。tracks=[], 全在 orphans。
    """
    import os  # noqa: PLC0415

    def _decode(p: object) -> str:
        if isinstance(p, (bytes, memoryview)):
            return bytes(p).decode("utf-8", errors="replace")
        return p or ""

    dir_path = _decode_local_album_mbid(mbid)
    if not dir_path:
        raise HTTPException(400, detail="bad local mbid")

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """SELECT id, path, title, format, bitrate, length,
                          artist, mb_artistid, mb_albumartistid
                   FROM beets.items
                   WHERE CAST(path AS TEXT) LIKE :p"""
            ),
            {"p": dir_path.rstrip("/") + "/%"},
        ).all()

        # 这一坨 items 的代表艺人：取出现最多的 mb_artistid
        from collections import Counter  # noqa: PLC0415
        artist_mbids = [
            (r.mb_albumartistid or r.mb_artistid or "") for r in rows
        ]
        artist_mbids = [a for a in artist_mbids if a]
        artist_mbid = ""
        artist_name = ""
        if artist_mbids:
            artist_mbid = Counter(artist_mbids).most_common(1)[0][0]
            a = conn.execute(
                text("SELECT name FROM mb_artist WHERE mbid=:m"),
                {"m": artist_mbid},
            ).first()
            if a:
                artist_name = a.name or ""
        if not artist_name:
            artist_name = next(
                (r.artist for r in rows if r.artist), ""
            )

    orphan_items = []
    for r in rows:
        path = _decode(r.path)
        orphan_items.append({
            "id": int(r.id),
            "title": r.title or "",
            "format": (r.format or "").upper(),
            "bitrate_kbps": int((r.bitrate or 0) // 1000),
            "length_s": float(r.length or 0),
            "path": path,
            "filename": os.path.basename(path),
        })
    orphan_items.sort(key=lambda x: x["filename"])

    return {
        "release_group_mbid": mbid,
        "title": os.path.basename(dir_path) or "(unknown)",
        "artist_mbid": artist_mbid,
        "artist": artist_name,
        "first_release_date": "",
        "primary_type": "Local",
        "tracks": [],
        "orphan_items": orphan_items,
        "owned_count": 0,
        "total_count": 0,
        "is_local": False,
        "dirs": [dir_path] if rows else [],
    }


@router.get("/release-groups/{mbid}/playable")
async def release_group_playable(mbid: str) -> dict:
    """MB-driven 专辑视图：返回 canonical MB 曲目表，每首附 bound_item（自动选的最佳本地副本）+ alternatives_count。

    "MusicTidy 是 player，不是 file manager" —— iOS 端的 album 详情就读这个，
    用户看到的永远是 MB 那张专辑（12 首就 12 首），不是 disk 上一堆重复 items。

    bound_item 自动选规则：
      1. 同 recording_mbid 的 items 里
      2. 优先 lossless/原文件（FLAC > WAV > APE 等），其次按 bitrate 高的

    localdir-<base64> 这种合成 mbid 走兜底分支，返回纯 orphan 视图。
    """
    if mbid.startswith(_LOCAL_DIR_PREFIX):
        return _local_dir_playable(mbid)

    import json  # noqa: PLC0415

    # 复用 tracks endpoint 的缓存逻辑：先从 mb_release_group.tracks_json 拿
    with get_engine().connect() as conn:
        # release-group 元数据
        rg_row = conn.execute(
            text(
                """SELECT mbid, title, primary_type, first_release_date,
                          artist_mbid, tracks_json,
                          COALESCE(is_local, 0) AS is_local
                   FROM mb_release_group WHERE mbid=:m"""
            ),
            {"m": mbid},
        ).first()
        if not rg_row:
            raise HTTPException(404, detail="release-group not in cache; trigger /tracks first")
        rg = dict(rg_row._mapping)

        # artist 名
        artist_name = ""
        if rg.get("artist_mbid"):
            a = conn.execute(
                text("SELECT name FROM mb_artist WHERE mbid=:m"),
                {"m": rg["artist_mbid"]},
            ).first()
            if a:
                artist_name = a.name or ""

        tracks_cached = rg.get("tracks_json")

    # 缓存里没有 MB 曲目 → 直接复用 /tracks 的逻辑去拉一次（自动 + 缓存）
    if not tracks_cached:
        try:
            sub = await release_group_tracks(mbid)
            mb_tracks = sub.get("tracks") or []
        except HTTPException:
            mb_tracks = []
    else:
        try:
            mb_tracks = json.loads(tracks_cached)
        except (TypeError, ValueError):
            mb_tracks = []

    import os  # noqa: PLC0415

    def _decode(p: object) -> str:
        if isinstance(p, bytes):
            return p.decode("utf-8", errors="replace")
        return p or ""

    with get_engine().connect() as conn:

        # 一次性把所有同 release-group items 拉出来按 recording_mbid 索引
        rows = conn.execute(
            text(
                """SELECT id, mb_trackid, title, format, bitrate, length, path
                   FROM beets.items
                   WHERE mb_releasegroupid = :rg"""
            ),
            {"rg": mbid},
        ).all()
        # MB canonical recording mbid 集合 —— 只有真的在 MB 曲目表里的 mb_trackid
        # 才算 "bound"。MB 拉不到的合成专辑、给错 mb_trackid 的本地文件，
        # 这里都视为未匹配，能进 orphan 列表
        mb_recording_set = {
            t.get("recording_mbid", "") for t in mb_tracks if t.get("recording_mbid")
        }

        by_recording: dict[str, list[dict]] = {}
        bound_ids: set[int] = set()
        item_dirs: list[str] = []
        for r in rows:
            rec = r.mb_trackid or ""
            path = _decode(r.path)
            if path:
                item_dirs.append(os.path.dirname(path))
            if not rec:
                continue
            cand = {
                "id": int(r.id),
                "title": r.title or "",
                "format": (r.format or "").upper(),
                "bitrate_kbps": int((r.bitrate or 0) // 1000),
                "length_s": float(r.length or 0),
                "path": path,
            }
            by_recording.setdefault(rec, []).append(cand)
            # 只有 mb_trackid 在 MB canonical 曲目里时才算真的 "bound"；否则当 orphan
            if rec in mb_recording_set:
                bound_ids.add(int(r.id))

        # 找这张专辑 items 的"主文件夹"（出现次数最多的 dir） —— 用户的
        # 文件一般按 album 一个文件夹放，"同文件夹未识别"的就是漏匹的歌
        orphan_items: list[dict] = []
        if item_dirs:
            from collections import Counter  # noqa: PLC0415
            main_dir = Counter(item_dirs).most_common(1)[0][0]
            # 拉同文件夹下所有 items（含没绑到这张 rg 的）
            # beets items.path 是 BLOB；SQLite LIKE 对 BLOB 不生效，要 CAST 成 TEXT
            folder_rows = conn.execute(
                text(
                    """SELECT id, mb_releasegroupid, mb_trackid, title, format,
                              bitrate, length, path
                       FROM beets.items
                       WHERE CAST(path AS TEXT) LIKE :p"""
                ),
                {"p": main_dir.rstrip("/") + "/%"},
            ).all()
            for r in folder_rows:
                if int(r.id) in bound_ids:
                    continue
                # 排除明显跨专辑误入的（绑到别的 rg）
                if (r.mb_releasegroupid or "") not in ("", mbid):
                    continue
                orphan_items.append({
                    "id": int(r.id),
                    "title": r.title or "",
                    "format": (r.format or "").upper(),
                    "bitrate_kbps": int((r.bitrate or 0) // 1000),
                    "length_s": float(r.length or 0),
                    "path": _decode(r.path),
                    "filename": os.path.basename(_decode(r.path)),
                })

    # 给每首 MB 曲目挑最佳 item
    def best(candidates: list[dict]) -> dict | None:
        if not candidates:
            return None
        # 用 FORMAT_PRIORITY，越小越好；同优先比 bitrate
        def score(c: dict) -> tuple[int, int]:
            return (FORMAT_PRIORITY.get(c["format"], 99), -c["bitrate_kbps"])
        return sorted(candidates, key=score)[0]

    tracks_out: list[dict] = []
    for t in mb_tracks:
        rec_mbid = t.get("recording_mbid") or ""
        cands = by_recording.get(rec_mbid, [])
        bound = best(cands)
        tracks_out.append({
            "disc": int(t.get("disc", 1) or 1),
            "position": int(t.get("position", 0) or 0),
            "recording_mbid": rec_mbid,
            "title": t.get("title", ""),
            "length_s": float(t.get("length_s", 0)),
            "bound_item": bound,
            "alternatives_count": max(0, len(cands) - (1 if bound else 0)),
        })

    # 来源目录：从 item_dirs 收集 distinct，按出现次数排
    from collections import Counter as _Counter  # noqa: PLC0415
    dirs_counter = _Counter(item_dirs)
    dirs_list = [d for d, _ in dirs_counter.most_common()]

    return {
        "release_group_mbid": mbid,
        "title": rg.get("title", ""),
        "artist_mbid": rg.get("artist_mbid", ""),
        "artist": artist_name,
        "first_release_date": rg.get("first_release_date", ""),
        "primary_type": rg.get("primary_type", ""),
        "tracks": tracks_out,
        "orphan_items": orphan_items,
        "owned_count": sum(1 for t in tracks_out if t["bound_item"]),
        "total_count": len(tracks_out),
        "is_local": bool(rg.get("is_local") or 0),
        "dirs": dirs_list,
    }


@router.post("/playlist/genre-summary")
async def playlist_genre_summary(payload: dict) -> dict:
    """给 iOS 端"按风格搜图"用：传 item_ids，返回聚合后的 top genres + 一个建议
    的搜图关键词。

    流程：item_ids → 各自的 mb_albumartistid（或 mb_artistid 兜底） → 查
    mb_artist.genres（已经是 [{name,count}] 排序好的）→ 跨所有艺人投票累加 →
    挑 top tags → 按 mood 词典套一个"album art" 类的后缀。
    """
    item_ids = payload.get("item_ids") or []
    if not isinstance(item_ids, list) or not item_ids:
        return {"top_genres": [], "suggested_query": ""}

    placeholders = ",".join(f":i{idx}" for idx in range(len(item_ids)))
    params = {f"i{idx}": int(v) for idx, v in enumerate(item_ids) if isinstance(v, int)}
    if not params:
        return {"top_genres": [], "suggested_query": ""}

    sql = f"""
        SELECT DISTINCT COALESCE(NULLIF(mb_albumartistid, ''), mb_artistid) AS mbid
        FROM beets.items
        WHERE id IN ({placeholders})
          AND COALESCE(NULLIF(mb_albumartistid, ''), mb_artistid) != ''
    """
    with get_engine().connect() as conn:
        artist_mbids = [r[0] for r in conn.execute(text(sql), params).all()]

        if not artist_mbids:
            return {"top_genres": [], "suggested_query": ""}

        # 拿这些艺人的 genres JSON
        a_placeholders = ",".join(f":a{idx}" for idx in range(len(artist_mbids)))
        a_params = {f"a{idx}": m for idx, m in enumerate(artist_mbids)}
        rows = conn.execute(
            text(
                f"SELECT mbid, genres FROM mb_artist WHERE mbid IN ({a_placeholders})"
            ),
            a_params,
        ).all()

    # 跨艺人投票累加
    bag: dict[str, int] = {}
    for _, genres_json in rows:
        if not genres_json:
            continue
        try:
            tags = json.loads(genres_json)
        except json.JSONDecodeError:
            continue
        for t in tags:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            try:
                cnt = int(t.get("count") or 1)
            except (ValueError, TypeError):
                cnt = 1
            bag[name] = bag.get(name, 0) + cnt

    if not bag:
        return {"top_genres": [], "suggested_query": ""}

    top = sorted(bag.items(), key=lambda x: -x[1])[:5]
    top_names = [n for n, _ in top]

    # 风格 → 搜图后缀
    bag_text = " ".join(top_names).lower()
    if any(k in bag_text for k in ("metal", "punk", "hardcore", "industrial",
                                    "doom", "grindcore")):
        mood = "intense dark"
    elif any(k in bag_text for k in ("jazz", "classical", "ambient", "lounge",
                                      "soundtrack", "bossa")):
        mood = "minimal elegant"
    elif any(k in bag_text for k in ("pop", "dance", "electronic", "edm",
                                      "house", "synth")):
        mood = "vibrant neon"
    elif any(k in bag_text for k in ("folk", "country", "acoustic",
                                      "americana", "bluegrass")):
        mood = "warm vintage"
    elif any(k in bag_text for k in ("rock", "indie", "alternative",
                                      "post-rock")):
        mood = "moody atmospheric"
    elif any(k in bag_text for k in ("hip hop", "rap", "trap")):
        mood = "urban street"
    else:
        mood = "aesthetic"

    # 头两个 tag 拼前缀，避免太长
    query = f"{' '.join(top_names[:2])} {mood} album cover"
    return {
        "top_genres": top_names,
        "suggested_query": query,
        "artist_mbid_count": len(rows),
    }


@router.post("/items/{item_id}/shazam-feedback")
async def shazam_feedback(item_id: int, payload: dict) -> dict:
    """iOS 端用 ShazamKit 识别后回传结果。
    payload: { title, artist, isrc?, apple_music_id?, album? }

    本端能做的事：
      1. 直接更新 beets 的 title/artist（用户已经选 Shazam = 信任 Shazam 元数据）
      2. 若给 isrc → 走 MB 的 ISRC 索引查 recording_mbid，命中则补 mb_trackid
      3. 若给 apple_music_id → 留作后续 enrichment

    iOS 拿这个 endpoint 等于"帮服务器整理"——Shazam catalog 远比 AcoustID 全，
    特别是华语 / 日语商业曲。
    """
    title = (payload.get("title") or "").strip()
    artist = (payload.get("artist") or "").strip()
    isrc = (payload.get("isrc") or "").strip().upper()
    apple_music_id = (payload.get("apple_music_id") or "").strip()
    album = (payload.get("album") or "").strip() or None
    if not title:
        return {"ok": False, "reason": "title empty"}

    s = get_settings()
    if not s.beets_db.exists():
        return {"ok": False, "reason": "beets DB 不存在"}

    # 直接更新 beets.items 的 title / artist
    with get_engine().begin() as conn:
        # 通过 ATTACH 的 beets 库写
        updates = ["title = :title", "artist = :artist"]
        params = {"title": title, "artist": artist, "id": item_id}
        if album:
            updates.append("album = :album")
            params["album"] = album
        conn.execute(
            text(f"UPDATE beets.items SET {', '.join(updates)} WHERE id = :id"),
            params,
        )

    # TODO: ISRC → MB recording lookup worker。Shazam 的 isrc 在 MB 大概率有索引，
    # 走 musicbrainzngs.search_recordings(isrc=...) 拿 recording mbid 就能 mb_trackid 自动落地。
    return {
        "ok": True,
        "updated": {"title": title, "artist": artist, "isrc": isrc or None},
        "isrc_lookup_todo": bool(isrc),
    }


@router.post("/items/{item_id}/refingerprint")
async def refingerprint_item(item_id: int) -> dict:
    """单首重跑 AcoustID 指纹识别. UI 端的"重新指纹匹配"按钮.

    工作流: 把这一条 push 到 fingerprint queue，worker 会重新算指纹 + 查 MB 并写回。
    """
    from app.workers import queue  # noqa: PLC0415
    queue.enqueue("fingerprint", {"item_id": item_id})
    return {"ok": True, "queued": item_id}


@router.post("/items/{item_id}/identify")
async def identify_item(item_id: int, payload: dict) -> dict:
    """人工指认：把这个 item 绑到某个 MB recording。

    payload: {
        "recording_mbid": str (必填),
        "title": str?, "artist": str?, "album": str?, "track": int?,
        "release_group_mbid": str?, "artist_mbid": str?, "album_artist_mbid": str?
    }
    """
    from app import beets_bridge  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)

    # 拿 canonical album-artist 名字写进 it.albumartist。
    # organize 的 path template 用的就是这个字段（$albumartist/$album/...），
    # 不强制覆盖的话 tag 里的旧名会一直占着，目录永远不会被规范化。
    album_artist_name = _canonical_artist_name(payload.get("album_artist_mbid"))

    ok = beets_bridge.set_item_meta(
        lib, item_id,
        title=payload.get("title"),
        artist=payload.get("artist"),
        album=payload.get("album"),
        track=payload.get("track"),
        track_mbid=payload.get("recording_mbid"),
        releasegroup_mbid=payload.get("release_group_mbid"),
        artist_mbid=payload.get("artist_mbid"),
        album_artist_mbid=payload.get("album_artist_mbid"),
        album_artist=album_artist_name,
    )
    if not ok:
        raise HTTPException(404, detail="item not found")

    # 顺手把这首歌的指纹存进我们自己的指纹库（source=manual）
    from app import fingerprint_db  # noqa: PLC0415

    path = beets_bridge.get_item_path(lib, item_id)
    if path is not None and path.exists():
        await asyncio.to_thread(
            fingerprint_db.extract_and_save,
            item_id, path,
            recording_mbid=payload.get("recording_mbid"),
            title=payload.get("title"),
            artist=payload.get("artist"),
            album=payload.get("album"),
            source="manual",
        )

    return {"ok": True, "item_id": item_id}


@router.get("/release-groups/{mbid}")
async def get_release_group(mbid: str) -> dict:
    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                """SELECT rg.mbid, rg.title, rg.primary_type,
                          rg.first_release_date, rg.cover_url, rg.artist_mbid,
                          a.name AS artist_name
                   FROM mb_release_group rg
                   LEFT JOIN mb_artist a ON a.mbid = rg.artist_mbid
                   WHERE rg.mbid = :m"""
            ),
            {"m": mbid},
        ).first()
    if not row:
        raise HTTPException(404, detail="release group not found")
    d = dict(row._mapping)
    d["cover_url"] = d.get("cover_url") or \
        f"https://coverartarchive.org/release-group/{mbid}/front-500"
    d["owned_items"] = 0
    return d


_LOCAL_DIR_PREFIX = "localdir-"


def _synth_local_album_mbid(dir_path: str) -> str:
    import base64  # noqa: PLC0415
    return _LOCAL_DIR_PREFIX + base64.urlsafe_b64encode(
        dir_path.encode("utf-8")
    ).decode("ascii").rstrip("=")


def _decode_local_album_mbid(mbid: str) -> str | None:
    if not mbid.startswith(_LOCAL_DIR_PREFIX):
        return None
    import base64  # noqa: PLC0415
    raw = mbid[len(_LOCAL_DIR_PREFIX):]
    pad = "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw + pad).decode("utf-8")
    except Exception:
        return None


@router.get("/artists/{mbid}/owned-albums")
async def owned_albums(mbid: str) -> dict:
    """给 Browse 用：返回此艺人参与的所有 release-group（带封面 URL）.

    "参与" = RG 本身就归这个艺人 (artist_mbid 匹配)，或者用户库里有这首
    歌的 albumartist/artist 是这个艺人 —— 合辑/OST 这类多艺人专辑能在每个
    参与艺人的 Browse 页都出现。

    末尾再追加"本地兜底"专辑：mb_releasegroupid 为空但 artist 是这位的
    items，按主文件夹聚合成虚拟专辑，mbid 用 'localdir-<base64(dir)>'
    格式编码。前端走同一套 /playable/{mbid}，server 端识别 prefix 后
    返回纯 orphan 视图。这样用户能至少看到那些 fingerprint 没识别的专辑。
    """
    import os  # noqa: PLC0415
    from collections import defaultdict  # noqa: PLC0415

    def _decode_path(p: object) -> str:
        if isinstance(p, (bytes, memoryview)):
            return bytes(p).decode("utf-8", errors="replace")
        return p or ""

    # 特例：'__orphan__' 虚拟艺人 → 直接返回无 artist mbid 的 items 聚合
    if mbid == NO_ARTIST_MBID:
        with get_engine().connect() as conn:
            orphan_items = conn.execute(
                text(
                    """SELECT id, path
                       FROM beets.items
                       WHERE COALESCE(NULLIF(mb_albumartistid, ''),
                                      mb_artistid) = ''"""
                )
            ).all()
        by_dir_orphan: dict[str, list[Any]] = defaultdict(list)
        for r in orphan_items:
            p = _decode_path(r.path)
            if not p:
                continue
            d = os.path.dirname(p)
            if d:
                by_dir_orphan[d].append(r)
        result_orphan: list[dict] = []
        for d, its in sorted(by_dir_orphan.items()):
            title = os.path.basename(d) or "(unknown)"
            synth = _synth_local_album_mbid(d)
            result_orphan.append({
                "mbid": synth,
                "title": title,
                "primary_type": "",
                "secondary_types": "",
                "first_release_date": "",
                "cover_url": f"/api/v1/covers/release-group/{synth}/500",
                "owned_items": len(its),
                "is_local": True,
                "total_tracks": len(its),
            })
        return {"count": len(result_orphan), "albums": result_orphan}

    with get_engine().connect() as conn:
        # 兜底：is_local 列可能还没 migrate（仅查询时手动 add 一次，写过的版本会从下次开始稳定 SELECT）
        cols = {r[1] for r in conn.execute(
            text("PRAGMA table_info(mb_release_group)")
        ).all()}
        has_is_local = "is_local" in cols
        is_local_sel = "rg.is_local" if has_is_local else "0 AS is_local"
        # rg.artist_mbid 直接关联当前艺人时也算
        # is_local 专辑哪怕还没绑 item 也算这位艺人的（用户刚建的空壳要能看到）；
        # 真 MB 专辑必须有 item 命中才进列表
        if has_is_local:
            artist_only_clause = "OR (rg.is_local = 1 AND rg.artist_mbid = :m)"
        else:
            artist_only_clause = ""
        # 多带一个 tracks_json，前端能算 'total_tracks' 决定是不是完整
        rgs = conn.execute(
            text(
                f"""SELECT
                       rg.mbid, rg.title, rg.primary_type, rg.secondary_types,
                       rg.first_release_date, rg.cover_url,
                       rg.tracks_json,
                       (SELECT COUNT(*) FROM beets.items i
                        WHERE i.mb_releasegroupid = rg.mbid) AS owned_items,
                       {is_local_sel}
                   FROM mb_release_group rg
                   WHERE EXISTS (
                             SELECT 1 FROM beets.items i
                             WHERE i.mb_releasegroupid = rg.mbid
                               AND (rg.artist_mbid = :m
                                    OR COALESCE(NULLIF(i.mb_albumartistid, ''),
                                                i.mb_artistid) = :m)
                         )
                      {artist_only_clause}
                   ORDER BY rg.first_release_date IS NULL,
                            rg.first_release_date"""
            ),
            {"m": mbid},
        ).all()
        result: list[dict] = []
        for r in rgs:
            d = dict(r._mapping)
            is_local_flag = bool(d.get("is_local") or 0)
            d["is_local"] = is_local_flag
            # 算 total_tracks：tracks_json 拉到了就 len(json) 当 canonical 数,
            # 没拉到就 None (前端别判完整 / 不完整)
            tj = d.pop("tracks_json", None)
            total = None
            if tj:
                try:
                    parsed = json.loads(tj)
                    if isinstance(parsed, list):
                        total = len(parsed)
                except (TypeError, ValueError):
                    total = None
            d["total_tracks"] = total
            if is_local_flag:
                # 本地策划专辑没 CAA 封面，前端直接走我们的 cover endpoint 拿 SVG
                d["cover_url"] = f"/api/v1/covers/release-group/{d['mbid']}/500"
            else:
                d["cover_url"] = d.get("cover_url") or \
                    f"https://coverartarchive.org/release-group/{d['mbid']}/front-500"
            result.append(d)

        # —— 本地兜底：artist 命中、但 release-group 没识别的 items ——
        local_items = conn.execute(
            text(
                """SELECT id, path, album
                   FROM beets.items
                   WHERE COALESCE(NULLIF(mb_albumartistid, ''),
                                  mb_artistid) = :m
                     AND (mb_releasegroupid IS NULL OR mb_releasegroupid = '')"""
            ),
            {"m": mbid},
        ).all()

    by_dir: dict[str, list[Any]] = defaultdict(list)
    for r in local_items:
        p = _decode_path(r.path)
        if not p:
            continue
        d = os.path.dirname(p)
        if d:
            by_dir[d].append(r)

    for d, its in sorted(by_dir.items()):
        title = os.path.basename(d) or "(unknown)"
        synth = _synth_local_album_mbid(d)
        result.append({
            "mbid": synth,
            "title": title,
            "primary_type": "",
            "secondary_types": "",
            "first_release_date": "",
            "cover_url": f"/api/v1/covers/release-group/{synth}/500",
            "owned_items": len(its),
            "is_local": True,
            "total_tracks": len(its),  # localdir 本地兜底视图: 拥有的就是全部
        })

    return {"count": len(result), "albums": result}
