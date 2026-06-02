"""单用户 session 认证.

设计：
- .env 里一个 APP_PASSWORD（明文，单用户用 secrets.compare_digest 安全比较）
- POST /api/v1/auth/login {password} → 返回 token + 设 cookie
- 所有受保护路径：先看 Authorization: Bearer xxx，再看 cookie session_token
- iOS 用 Bearer（存 Keychain），浏览器走 cookie，同一个 token 表

未设 APP_PASSWORD → 整个 app 跳过 auth（dev 默认，方便本地试验）.
"""

from __future__ import annotations

import secrets
import time

from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine

# 这些路径 prefix 永远不查 auth
PUBLIC_PATH_PREFIXES = (
    "/login",
    "/api/v1/auth/",
    "/static/",
    "/healthz",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def is_public_path(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PATH_PREFIXES)


def auth_disabled() -> bool:
    """没配密码 = 整个 app 公开（dev）."""
    return not bool(get_settings().app_password)


def check_password(password: str) -> bool:
    s = get_settings()
    if not s.app_password:
        return False
    return secrets.compare_digest(
        password.encode("utf-8"),
        s.app_password.encode("utf-8"),
    )


def create_session(user_agent: str = "") -> tuple[str, int]:
    """新建 session，返回 (token, expires_at_unix)."""
    s = get_settings()
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    exp = now + s.session_ttl_days * 86400
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO auth_session
                       (token, created_at, expires_at, last_used_at, user_agent)
                   VALUES (:t, :n, :e, :n, :ua)"""
            ),
            {"t": token, "n": now, "e": exp, "ua": (user_agent or "")[:200]},
        )
    return token, exp


def validate_token(token: str) -> bool:
    """token 有效且未过期 → True，刷 last_used_at."""
    if not token:
        return False
    now = int(time.time())
    with get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT expires_at FROM auth_session WHERE token=:t"),
            {"t": token},
        ).first()
        if not row or int(row.expires_at) < now:
            return False
        conn.execute(
            text("UPDATE auth_session SET last_used_at=:n WHERE token=:t"),
            {"n": now, "t": token},
        )
    return True


def revoke_token(token: str) -> None:
    if not token:
        return
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM auth_session WHERE token=:t"), {"t": token})


def gc_expired_sessions() -> int:
    """删过期 token；返回删了几条."""
    with get_engine().begin() as conn:
        r = conn.execute(
            text("DELETE FROM auth_session WHERE expires_at < :n"),
            {"n": int(time.time())},
        )
        return r.rowcount or 0


def extract_token_from_request(request) -> str | None:
    """优先看 Authorization: Bearer，再看 cookie session_token."""
    h = request.headers.get("authorization", "")
    if h.startswith("Bearer "):
        return h[len("Bearer "):].strip() or None
    return request.cookies.get("session_token")
