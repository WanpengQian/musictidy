"""库浏览 / dashboard JSON endpoints (P1).

数据模型：
- 一个 artist 出现在 dashboard 上的条件：beets 里至少一个 item 挂了它的 MBID
- "拥有"一张专辑 = 至少一个 item 的 mb_releasegroupid 等于这个 release-group
- 完整度 = 拥有数 / 总数 (在指定的 primary_type 过滤下)
"""

from __future__ import annotations

import asyncio
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

        # 曲目前缀匹配（beets）
        if q:
            track_rows = conn.execute(
                text(
                    """SELECT id, title, artist, album, format
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
        return {"id": r.id, "title": r.title,
                "artist": r.artist, "album": r.album, "format": r.format}

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
            COUNT(*) AS items_count
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
        a.name,
        a.sort_name,
        a.country
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
        out.append(m)
    return out


@router.get("/artists")
async def list_artists(
    sort: str = Query("completeness", regex="^(completeness|owned|alpha|items)$"),
    filter: str = Query("all", regex="^(all|incomplete|complete|no_mb_cache)$"),
) -> dict:
    rows = _list_artists_rows(sort, filter)
    return {"sort": sort, "filter": filter, "count": len(rows), "artists": rows}


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


PLAYABLE_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".aiff", ".alac"}
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

    # 用户从手机上传的自定义封面优先
    pref = _cover_pref(mbid)
    if pref == "custom":
        custom = _custom_cover_path(mbid)
        if custom is not None:
            # 所有尺寸都返回同一张原图，让客户端自己缩放
            ext = custom.suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "webp": "image/webp",
                    "heic": "image/heic"}.get(ext, "application/octet-stream")
            return FileResponse(custom, media_type=mime)

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
        raise HTTPException(404, detail="cover not found")
    if resp.status_code != 200:
        raise HTTPException(502, detail=f"upstream HTTP {resp.status_code}")

    cache_path.write_bytes(resp.content)
    log.info("cached cover rg=%s size=%d (%d KB)",
             mbid, size, len(resp.content) // 1024)

    # 500 尺寸的封面同时写一份 cover.jpg 到对应的专辑目录
    if size == 500 and s.allow_file_writes:
        _write_cover_into_album_dirs(mbid, resp.content)

    return FileResponse(cache_path, media_type="image/jpeg")


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


@router.get("/artists/{mbid}/owned-albums")
async def owned_albums(mbid: str) -> dict:
    """给 iOS Browse 用：返回此艺人参与的所有 release-group（带封面 URL）.

    "参与" = RG 本身就归这个艺人 (artist_mbid 匹配)，或者用户库里有这首
    歌的 albumartist/artist 是这个艺人 —— 合辑/OST 这类多艺人专辑能在每个
    参与艺人的 Browse 页都出现。
    """
    with get_engine().connect() as conn:
        rgs = conn.execute(
            text(
                """SELECT
                       rg.mbid, rg.title, rg.primary_type, rg.secondary_types,
                       rg.first_release_date, rg.cover_url,
                       (SELECT COUNT(*) FROM beets.items i
                        WHERE i.mb_releasegroupid = rg.mbid) AS owned_items
                   FROM mb_release_group rg
                   WHERE EXISTS (
                       SELECT 1 FROM beets.items i
                       WHERE i.mb_releasegroupid = rg.mbid
                         AND (rg.artist_mbid = :m
                              OR COALESCE(NULLIF(i.mb_albumartistid, ''),
                                          i.mb_artistid) = :m)
                   )
                   ORDER BY rg.first_release_date IS NULL,
                            rg.first_release_date"""
            ),
            {"m": mbid},
        ).all()
        result = []
        for r in rgs:
            d = dict(r._mapping)
            d["cover_url"] = d.get("cover_url") or \
                f"https://coverartarchive.org/release-group/{d['mbid']}/front-500"
            result.append(d)
        return {"count": len(result), "albums": result}
