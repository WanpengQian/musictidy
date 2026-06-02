"""登录 / token / cookie / 中间件全套测试."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


# ── 默认 conftest 给 APP_PASSWORD 留空 → auth disabled ──────────
def test_auth_disabled_lets_everything_through(client):
    assert client.get("/api/v1/admin/stats").status_code == 200
    assert client.get("/artists").status_code == 200
    me = client.get("/api/v1/auth/me").json()
    assert me["auth_disabled"] is True


# ── 启用 password 的 fixture ────────────────────────────────────
@pytest.fixture
def auth_client(env, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "secret123")
    from app import config

    config._settings = None
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_protected_api_returns_401_without_token(auth_client):
    r = auth_client.get("/api/v1/admin/stats")
    assert r.status_code == 401
    assert r.json()["detail"] == "unauthorized"


def test_protected_html_redirects_to_login(auth_client):
    r = auth_client.get("/artists", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]
    assert "next=/artists" in r.headers["location"]


def test_public_paths_remain_open(auth_client):
    assert auth_client.get("/login").status_code == 200
    assert auth_client.get("/healthz").status_code == 200
    assert auth_client.get("/docs").status_code == 200
    assert auth_client.get("/static/app.css").status_code == 200


def test_login_wrong_password_returns_401(auth_client):
    r = auth_client.post("/api/v1/auth/login", json={"password": "wrong"})
    assert r.status_code == 401


def test_login_correct_returns_token_and_sets_cookie(auth_client):
    r = auth_client.post("/api/v1/auth/login", json={"password": "secret123"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["expires_at"] > 0
    # cookie 也设了
    assert "session_token" in r.cookies


def test_bearer_token_unlocks_api(auth_client):
    r = auth_client.post("/api/v1/auth/login", json={"password": "secret123"})
    token = r.json()["token"]
    # 新连接不带 cookie，纯 bearer 头
    r2 = auth_client.get(
        "/api/v1/admin/stats",
        headers={"Authorization": f"Bearer {token}"},
        cookies={},   # 不带 cookie
    )
    assert r2.status_code == 200


def test_cookie_unlocks_html(auth_client):
    auth_client.post("/api/v1/auth/login", json={"password": "secret123"})
    # cookie 留在 client 里，下一个 GET 应该过
    r = auth_client.get("/artists")
    assert r.status_code == 200


def test_logout_revokes_token(auth_client):
    r = auth_client.post("/api/v1/auth/login", json={"password": "secret123"})
    token = r.json()["token"]
    # logout
    auth_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    # token 不再有效
    r2 = auth_client.get(
        "/api/v1/admin/stats",
        headers={"Authorization": f"Bearer {token}"},
        cookies={},
    )
    assert r2.status_code == 401


def test_form_login_redirects_and_sets_cookie(auth_client):
    r = auth_client.post(
        "/login",
        data={"password": "secret123", "next": "/duplicates"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/duplicates"
    assert "session_token" in r.cookies


def test_me_endpoint_authed(auth_client):
    auth_client.post("/api/v1/auth/login", json={"password": "secret123"})
    r = auth_client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["ok"] is True
