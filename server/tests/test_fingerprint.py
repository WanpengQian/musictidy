"""Fingerprint worker —— mock 掉 fpcalc + AcoustID HTTP."""

from __future__ import annotations

import pytest

from app.workers import fingerprint, queue


@pytest.mark.asyncio
async def test_no_api_key_skips_quietly(env, monkeypatch, tmp_path):
    """没配 ACOUSTID_API_KEY → 任务静默完成，不重试，不抛."""
    monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
    # reset settings cache
    from app import config

    config._settings = None
    fingerprint._warned_no_key = False

    await fingerprint.handle_fingerprint({"item_id": 1})  # 不应抛
    # 没 enqueue 后续任务
    assert queue.counts_by_status()["queued"] == 0


@pytest.mark.asyncio
async def test_match_writes_mbids_and_enqueues_artist(env, monkeypatch, tmp_path):
    """有 API key + AcoustID 返回高分匹配 → 写回 beets，排 mb_fetch_artist."""
    monkeypatch.setenv("ACOUSTID_API_KEY", "test-key")
    from app import config

    config._settings = None

    fake_path = tmp_path / "fake.flac"
    fake_path.write_bytes(b"fake audio")  # 内容不重要，我们 mock 了 fpcalc

    # mock beets_bridge
    calls = {}

    def fake_get_item_path(lib, item_id):
        return fake_path

    def fake_set_mb_ids(lib, item_id, **kwargs):
        calls["set_mb_ids"] = (item_id, kwargs)
        return True

    def fake_get_library(db_path, music_root):
        return object()

    monkeypatch.setattr(fingerprint.beets_bridge, "get_item_path", fake_get_item_path)
    monkeypatch.setattr(fingerprint.beets_bridge, "set_mb_ids", fake_set_mb_ids)
    monkeypatch.setattr(fingerprint.beets_bridge, "get_library", fake_get_library)

    # 新签名带 current_album 参数
    def fake_lookup(path, api_key, current_album=""):
        return (0.95, "rec-mbid", "rg-mbid", "song-artist-mbid", "album-artist-mbid")

    monkeypatch.setattr(fingerprint, "_blocking_fingerprint_and_lookup", _async_proxy(fake_lookup))
    # 现在 handler 还会调 get_item_tags 拿 album 名
    monkeypatch.setattr(
        fingerprint.beets_bridge, "get_item_tags",
        lambda lib, item_id: {"title": "", "artist": "", "albumartist": "",
                              "album": "OK Computer", "track": 1, "year": 0, "length": 0.0},
    )

    await fingerprint.handle_fingerprint({"item_id": 42})

    # 写回的字段对
    item_id, kwargs = calls["set_mb_ids"]
    assert item_id == 42
    assert kwargs["track_mbid"] == "rec-mbid"
    assert kwargs["releasegroup_mbid"] == "rg-mbid"
    assert kwargs["artist_mbid"] == "song-artist-mbid"
    assert kwargs["album_artist_mbid"] == "album-artist-mbid"

    # 排了 mb_fetch_artist
    task = queue.claim_one()
    assert task is not None
    assert task.kind == "mb_fetch_artist"
    assert task.payload == {"artist_mbid": "album-artist-mbid"}


@pytest.mark.asyncio
async def test_low_score_no_write(env, monkeypatch, tmp_path):
    """AcoustID 无匹配 + item 无 tag → 不写回，不排后续."""
    monkeypatch.setenv("ACOUSTID_API_KEY", "test-key")
    from app import config

    config._settings = None

    fake_path = tmp_path / "fake.flac"
    fake_path.write_bytes(b"x")

    def fake_get_library(db_path, music_root):
        return object()

    def fake_get_item_path(lib, item_id):
        return fake_path

    set_mb_called = []

    def fake_set_mb_ids(lib, item_id, **kwargs):
        set_mb_called.append(item_id)
        return True

    monkeypatch.setattr(fingerprint.beets_bridge, "get_library", fake_get_library)
    monkeypatch.setattr(fingerprint.beets_bridge, "get_item_path", fake_get_item_path)
    monkeypatch.setattr(fingerprint.beets_bridge, "set_mb_ids", fake_set_mb_ids)
    monkeypatch.setattr(
        fingerprint.beets_bridge, "get_item_tags",
        lambda lib, item_id: {"title": "", "artist": "", "albumartist": "",
                              "album": "", "track": 0, "year": 0, "length": 0.0},
    )

    monkeypatch.setattr(
        fingerprint,
        "_blocking_fingerprint_and_lookup",
        _async_proxy(lambda *_: None),
    )

    await fingerprint.handle_fingerprint({"item_id": 1})
    assert set_mb_called == []
    assert queue.counts_by_status()["queued"] == 0


@pytest.mark.asyncio
async def test_tag_fallback_writes_mbids(env, monkeypatch, tmp_path):
    """AcoustID 无匹配 + item 有 artist+album tag → tag 搜索 MB → 写回."""
    monkeypatch.setenv("ACOUSTID_API_KEY", "test-key")
    from app import config

    config._settings = None

    fake_path = tmp_path / "fake.ape"
    fake_path.write_bytes(b"x")

    def fake_get_library(*_a, **_kw):
        return object()

    def fake_get_item_path(lib, item_id):
        return fake_path

    def fake_get_item_tags(lib, item_id):
        return {
            "title": "", "artist": "Radiohead", "albumartist": "Radiohead",
            "album": "OK Computer", "track": 1, "year": 1997, "length": 240.0,
        }

    written = {}

    def fake_set_mb_ids(lib, item_id, **kwargs):
        written.update({"item_id": item_id, **kwargs})
        return True

    monkeypatch.setattr(fingerprint.beets_bridge, "get_library", fake_get_library)
    monkeypatch.setattr(fingerprint.beets_bridge, "get_item_path", fake_get_item_path)
    monkeypatch.setattr(fingerprint.beets_bridge, "get_item_tags", fake_get_item_tags)
    monkeypatch.setattr(fingerprint.beets_bridge, "set_mb_ids", fake_set_mb_ids)

    # AcoustID 返回无匹配
    monkeypatch.setattr(
        fingerprint, "_blocking_fingerprint_and_lookup",
        _async_proxy(lambda *_: None),
    )

    # tag 搜索返回结果
    async def fake_search(artist, album, **kw):
        assert artist == "Radiohead" and album == "OK Computer"
        return {
            "release_mbid": "rel-mbid",
            "releasegroup_mbid": "rg-mbid",
            "album_artist_mbid": "rh-mbid",
        }

    from app.workers import musicbrainz as mb_mod

    monkeypatch.setattr(mb_mod, "search_release_by_tags", fake_search)

    await fingerprint.handle_fingerprint({"item_id": 7})

    assert written["item_id"] == 7
    assert written["releasegroup_mbid"] == "rg-mbid"
    assert written["album_artist_mbid"] == "rh-mbid"
    # 排了 mb_fetch_artist
    t = queue.claim_one()
    assert t is not None
    assert t.payload == {"artist_mbid": "rh-mbid"}


def _async_proxy(sync_fn):
    """asyncio.to_thread 在测试里也能 work：返回一个 sync wrapper."""
    def wrapper(*args, **kwargs):
        return sync_fn(*args, **kwargs)
    return wrapper
