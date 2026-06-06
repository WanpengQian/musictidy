"""FastAPI 入口。直接 `python -m app.main` 启动。"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app import auth
from app.config import get_settings
from app.db import run_migrations

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    s = get_settings()
    s.ensure_dirs()
    run_migrations()

    from app import fingerprint_db, lyrics_db  # noqa: PLC0415
    fingerprint_db.ensure_table()
    lyrics_db.ensure_table()

    # 注册 task_queue handlers，然后启 scheduler + drain loop
    from app.workers import (  # noqa: PLC0415
        archive_extract,
        cue_split,
        fingerprint,
        musicbrainz,
        scheduler,
        sync_sidecars,
    )

    scheduler.register_handler("fingerprint", fingerprint.handle_fingerprint)
    scheduler.register_handler("mb_fetch_artist", musicbrainz.handle_fetch_artist)
    scheduler.register_handler("cue_split", cue_split.handle_cue_split)
    scheduler.register_handler("archive_extract", archive_extract.handle_archive_extract)
    scheduler.register_handler("sync_sidecars", sync_sidecars.handle_sync_sidecars)
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        from app import beets_bridge  # noqa: PLC0415
        beets_bridge.close_library()


class AuthMiddleware(BaseHTTPMiddleware):
    """所有受保护路径必须有有效 token；不然 API 401、网页跳 /login.

    APP_PASSWORD 没设 → 整个中间件直通（dev）.
    """

    async def dispatch(self, request: Request, call_next):
        if auth.auth_disabled():
            return await call_next(request)
        path = request.url.path
        if auth.is_public_path(path):
            return await call_next(request)
        tok = auth.extract_token_from_request(request)
        if not tok or not auth.validate_token(tok):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return RedirectResponse(
                f"/login?next={path}", status_code=303,
            )
        return await call_next(request)


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="MusicTidy",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(AuthMiddleware)

    # CORS：让浏览器从 app.musictidy.com / 本地开发 vite dev server 都能直连用户自己的 server。
    # iOS app fetch 不受 CORS，但 web app 必须开。
    # **必须在 AuthMiddleware 之后 add**（FastAPI 中间件后加先跑），否则 OPTIONS
    # preflight 会被 AuthMiddleware 401 掉。
    from fastapi.middleware.cors import CORSMiddleware  # noqa: PLC0415
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://app.musictidy.com",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Length", "Content-Range"],
    )

    # /api/v1/* 全部 no-store —— 库状态实时变化，任何端缓存都会让"明明加了歌却刷不出来"再发生
    @app.middleware("http")
    async def _no_store_api(request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # 静态文件 (htmx + css)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # JSON API
    from app.api import admin, auth as auth_api, library, playlist, wishlist  # noqa: PLC0415

    app.include_router(auth_api.router, prefix="/api/v1/auth", tags=["auth"])
    app.include_router(admin.router, prefix="/api/v1/admin", tags=["admin"])
    app.include_router(library.router, prefix="/api/v1", tags=["library"])
    app.include_router(playlist.router, prefix="/api/v1", tags=["playlist"])
    app.include_router(wishlist.router, prefix="/api/v1", tags=["wishlist"])

    # HTML 路由（dashboard）
    from app.api import web  # noqa: PLC0415

    app.include_router(web.router, tags=["web"])

    # TODO P2: from app.api import curation; app.include_router(...)

    @app.get("/healthz", response_class=JSONResponse)
    async def healthz():
        # 客户端"测试连接"靠 app 字段确认这是真 MusicTidy 服务器，
        # 而不是随便一个返回 200 的端口
        #
        # display_name / logo_url 是品牌字段（iOS 登录页用），无值时 iOS 端
        # 用默认 "MusicTidy" + 内置 App logo。
        has_logo = (
            s.server_logo_path is not None and s.server_logo_path.exists()
        )
        return {
            "ok": True,
            "app": "MusicTidy",
            "api_version": 1,
            "server_version": "0.3",
            "music_root": str(s.music_root),
            "data_dir": str(s.data_dir),
            "allow_file_writes": s.allow_file_writes,
            "display_name": s.server_display_name,
            "logo_url": "/api/v1/server/logo" if has_logo else None,
        }

    @app.get("/api/v1/server/logo")
    async def server_logo():
        """iOS 登录页拿这张图当 server logo；服务器没配则 404。
        无需 auth：登录前就要展示，跟 /healthz 一样属于公开标识。
        """
        from fastapi.responses import FileResponse  # noqa: PLC0415
        from fastapi import HTTPException  # noqa: PLC0415

        if s.server_logo_path is None or not s.server_logo_path.exists():
            raise HTTPException(404, "server logo not configured")
        return FileResponse(
            str(s.server_logo_path),
            # 一天 cache 足够；改 logo 不是热操作
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return app


app = create_app()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.bind_host,
        port=s.bind_port,
        reload=False,
        access_log=True,
    )


if __name__ == "__main__":
    main()
