"""HTML 路由 (Jinja + htmx) —— dashboard 入口."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from sqlalchemy import text

from app import auth
from app.api.library import (
    _list_album_duplicates,
    _list_artists_rows,
    _list_duplicate_groups,
    artist_detail,
)
from app.config import get_settings
from app.db import get_engine
from app.templates_engine import templates

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/artists", status_code=307)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str | None = None):
    if auth.auth_disabled():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"next": next, "error": error},
    )


@router.post("/login")
async def login_form(
    request: Request,
    password: str = Form(...),
    next: str = Form("/"),
):
    """浏览器的登录表单 → set cookie + 303 跳目标."""
    if not auth.check_password(password):
        return templates.TemplateResponse(
            request, "login.html",
            {"next": next, "error": "密码错误"},
            status_code=401,
        )
    token, exp = auth.create_session(request.headers.get("user-agent", ""))
    s = get_settings()
    resp = RedirectResponse(next or "/", status_code=303)
    resp.set_cookie(
        "session_token", token,
        max_age=max(0, exp - int(time.time())),
        httponly=True, samesite="lax",
        secure=s.cookie_secure, path="/",
    )
    return resp


@router.post("/logout")
async def logout_link(request: Request):
    tok = auth.extract_token_from_request(request)
    if tok:
        auth.revoke_token(tok)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session_token", path="/")
    return resp


@router.get("/artists", response_class=HTMLResponse)
async def artists_page(
    request: Request,
    sort: str = Query("completeness", regex="^(completeness|owned|alpha|items)$"),
    filter: str = Query("all", regex="^(all|incomplete|complete|no_mb_cache)$"),
):
    artists = _list_artists_rows(sort, filter)
    return templates.TemplateResponse(
        request, "artists.html",
        {"artists": artists, "sort": sort, "filter": filter},
    )


@router.get("/artists/{mbid}", response_class=HTMLResponse)
async def artist_detail_page(request: Request, mbid: str):
    try:
        data = await artist_detail(mbid)
    except HTTPException as e:
        if e.status_code == 404:
            return HTMLResponse(
                f"<p>artist mbid <code>{mbid}</code> 不存在，可能还没识别到这个艺人。</p>",
                status_code=404,
            )
        raise
    return templates.TemplateResponse(
        request, "artist_detail.html",
        {
            "artist": data["artist"],
            "items_in_library": data["items_in_library"],
            "release_groups": data["release_groups"],
        },
    )


@router.get("/organize", response_class=HTMLResponse)
async def organize_page(request: Request):
    from app import beets_bridge, organize  # noqa: PLC0415
    from app.config import get_settings as _gs  # noqa: PLC0415

    s = _gs()
    groups: list = []
    if s.beets_db.exists():
        lib = beets_bridge.get_library(s.beets_db, s.music_root)
        try:
            groups = organize.compute_preview(lib)
        except Exception:
            log.exception("organize.preview failed")
    return templates.TemplateResponse(
        request, "organize.html",
        {
            "groups": groups,
            "allow_file_writes": s.allow_file_writes,
        },
    )


@router.get("/archives", response_class=HTMLResponse)
async def archives_page(request: Request):
    """RAR/ZIP/7z 压缩包诊断页 —— /api/v1/admin/diagnose-archives 的 HTML 壳."""
    from app import archive  # noqa: PLC0415
    from app.config import get_settings as _gs  # noqa: PLC0415

    s = _gs()
    writes_ok = s.allow_file_writes
    unar_ok = archive.unar_available()

    pending: list[dict] = []
    extracted: list[dict] = []
    try:
        for arc in archive.detect_archives(s.music_root):
            rel = (
                str(arc.relative_to(s.music_root))
                if arc.is_relative_to(s.music_root)
                else str(arc)
            )
            size = arc.stat().st_size if arc.exists() else 0
            row = {"rel": rel, "size_mb": size / 1024 / 1024}
            if archive.is_already_extracted(arc):
                extracted.append(row)
            else:
                pending.append(row)
    except Exception:
        log.exception("archives page: detect_archives 失败")

    queue_counts: dict[str, int] = {}
    queue_rows: list[dict] = []
    try:
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
                       LIMIT 20"""
                )
            ).all():
                queue_rows.append(dict(r._mapping))
    except Exception:
        log.exception("archives page: 读 task_queue 失败")

    if not writes_ok:
        verdict = ("ALLOW_FILE_WRITES=false → worker 跳过所有解压。"
                   "改 .env 后重启服务。")
    elif not unar_ok and pending:
        verdict = ("unar 没装 → 没法解 RAR/7z。"
                   "apt/brew install unar 后 POST /api/v1/admin/scan。")
    elif not pending and not extracted:
        verdict = "music_root 里没找到任何 .rar/.zip/.7z。"
    elif not pending and extracted:
        verdict = f"OK — 全部 {len(extracted)} 个档已解过了。"
    elif pending and not queue_rows:
        verdict = (f"待解 {len(pending)} 个，task_queue 里还没排队。"
                   "POST /api/v1/admin/scan 触发。")
    elif pending and queue_counts.get("failed", 0):
        verdict = (f"待解 {len(pending)} 个，{queue_counts.get('failed', 0)} 个失败。"
                   "看下面 task 列表里的 last_error。")
    else:
        verdict = f"队列在跑：{queue_counts}。"

    return templates.TemplateResponse(
        request, "archives.html",
        {
            "verdict": verdict,
            "allow_file_writes": writes_ok,
            "unar_available": unar_ok,
            "music_root": str(s.music_root),
            "pending": pending,
            "extracted": extracted,
            "queue_counts": queue_counts,
            "queue_rows": queue_rows,
        },
    )


@router.get("/duplicates", response_class=HTMLResponse)
async def duplicates_page(request: Request):
    try:
        groups = _list_duplicate_groups()
    except Exception:
        log.exception("duplicates page failed (track-level)")
        groups = []
    try:
        albums = _list_album_duplicates()
    except Exception:
        log.exception("duplicates page failed (album-level)")
        albums = []
    return templates.TemplateResponse(
        request, "duplicates.html",
        {"groups": groups, "albums": albums},
    )


# /admin HTML 页已删 —— 管理动作直接走 /api/v1/admin/* (curl / Swagger /docs)
