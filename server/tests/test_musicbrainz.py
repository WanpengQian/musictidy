"""MusicBrainz worker —— mock musicbrainzngs."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text

from app.db import get_engine
from app.workers import musicbrainz


FAKE_ARTIST_PAYLOAD = {
    "artist": {
        "name": "Radiohead",
        "sort-name": "Radiohead",
        "country": "GB",
        "disambiguation": "",
        "release-group-list": [
            {
                "id": "rg-pablo-honey",
                "title": "Pablo Honey",
                "type": "Album",
                "first-release-date": "1993-02-22",
            },
            {
                "id": "rg-the-bends",
                "title": "The Bends",
                "type": "Album",
                "secondary-type-list": [],
                "first-release-date": "1995-03-13",
            },
            {
                "id": "rg-i-might-be-wrong",
                "title": "I Might Be Wrong: Live Recordings",
                "type": "Album",
                "secondary-type-list": ["Live"],
                "first-release-date": "2001-11-12",
            },
        ],
    }
}


@pytest.mark.asyncio
async def test_fetch_artist_upserts_artist_and_release_groups(env, monkeypatch):
    """Happy path: fetch artist + all release-groups stored."""

    def fake_get_artist_by_id(mbid, includes):
        assert includes == ["release-groups", "genres", "tags"]
        return FAKE_ARTIST_PAYLOAD

    monkeypatch.setattr(musicbrainz.musicbrainzngs, "get_artist_by_id", fake_get_artist_by_id)

    await musicbrainz.handle_fetch_artist({"artist_mbid": "abc-123"})

    with get_engine().connect() as conn:
        a = conn.execute(text("SELECT name, country FROM mb_artist WHERE mbid='abc-123'")).first()
        assert a is not None
        assert a.name == "Radiohead"
        assert a.country == "GB"

        rgs = conn.execute(
            text("SELECT mbid, title, primary_type, secondary_types FROM mb_release_group "
                 "WHERE artist_mbid='abc-123' ORDER BY first_release_date")
        ).all()
        assert len(rgs) == 3
        assert rgs[0].title == "Pablo Honey"
        assert rgs[0].primary_type == "Album"
        assert rgs[2].secondary_types == '["Live"]'


@pytest.mark.asyncio
async def test_fresh_artist_skipped(env, monkeypatch):
    """已经在缓存里且 stale_after 未到 → 不调 API."""
    now = int(time.time())
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO mb_artist (mbid, name, sort_name, country, disambiguation,
                                          fetched_at, stale_after)
                   VALUES ('xyz-999', 'X', 'X', '', '', :n, :sa)"""
            ),
            {"n": now, "sa": now + 9999},
        )

    called = {"count": 0}

    def fake_get_artist_by_id(*_a, **_kw):
        called["count"] += 1
        return FAKE_ARTIST_PAYLOAD

    monkeypatch.setattr(musicbrainz.musicbrainzngs, "get_artist_by_id", fake_get_artist_by_id)
    await musicbrainz.handle_fetch_artist({"artist_mbid": "xyz-999"})
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_404_response_does_not_retry(env, monkeypatch):
    """ResponseError（错的 mbid）→ 静默返回，不抛."""
    from musicbrainzngs import ResponseError

    def fake_get_artist_by_id(*_a, **_kw):
        raise ResponseError("404 — not found")

    monkeypatch.setattr(musicbrainz.musicbrainzngs, "get_artist_by_id", fake_get_artist_by_id)
    # 不应抛
    await musicbrainz.handle_fetch_artist({"artist_mbid": "bad-mbid"})


@pytest.mark.asyncio
async def test_network_error_retries(env, monkeypatch):
    """NetworkError → 抛出，让 queue 重试."""
    from musicbrainzngs import NetworkError

    def fake_get_artist_by_id(*_a, **_kw):
        raise NetworkError("connection refused")

    monkeypatch.setattr(musicbrainz.musicbrainzngs, "get_artist_by_id", fake_get_artist_by_id)
    with pytest.raises(NetworkError):
        await musicbrainz.handle_fetch_artist({"artist_mbid": "abc"})


@pytest.mark.asyncio
async def test_rate_limit_enforced(env, monkeypatch):
    """连续两次调用至少间隔 RATE_LIMIT_SEC."""

    def fake_get_artist_by_id(mbid, includes):
        return {"artist": {"name": "x", "release-group-list": []}}

    monkeypatch.setattr(musicbrainz.musicbrainzngs, "get_artist_by_id", fake_get_artist_by_id)

    start = time.monotonic()
    await musicbrainz.handle_fetch_artist({"artist_mbid": "a1"})
    await musicbrainz.handle_fetch_artist({"artist_mbid": "a2"})
    elapsed = time.monotonic() - start
    # 1.05s 间隔 —— 第一次几乎瞬时，第二次要等
    assert elapsed >= 1.0, f"rate limit not enforced: elapsed={elapsed}"
