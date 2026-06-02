"""登录 API（iOS 用 JSON Bearer，浏览器走 form + cookie 都接同一套 store）."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app import auth
from app.config import get_settings

router = APIRouter()


class LoginBody(BaseModel):
    password: str


def _set_cookie(response: Response, token: str, expires_at: int) -> None:
    s = get_settings()
    max_age = max(0, expires_at - int(time.time()))
    response.set_cookie(
        "session_token", token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=s.cookie_secure,
        path="/",
    )


@router.post("/login")
async def login_api(body: LoginBody, request: Request, response: Response) -> dict:
    """iOS / 编程客户端用。返回 token；同时也设 cookie（浏览器友好）.

    iOS 拿到 token 存 Keychain，后续 `Authorization: Bearer <token>` 发就行.
    """
    if not auth.check_password(body.password):
        raise HTTPException(401, detail="wrong password")
    token, exp = auth.create_session(request.headers.get("user-agent", ""))
    _set_cookie(response, token, exp)
    return {"token": token, "expires_at": exp}


@router.post("/logout")
async def logout_api(request: Request, response: Response) -> dict:
    tok = auth.extract_token_from_request(request)
    if tok:
        auth.revoke_token(tok)
    response.delete_cookie("session_token", path="/")
    return {"ok": True}


@router.get("/me")
async def whoami(request: Request) -> dict:
    """200 = 已登录；401 = 没登录。iOS 启动时调用做 token 健康检查."""
    if auth.auth_disabled():
        return {"ok": True, "auth_disabled": True}
    tok = auth.extract_token_from_request(request)
    if not tok or not auth.validate_token(tok):
        raise HTTPException(401, detail="not logged in")
    return {"ok": True}
