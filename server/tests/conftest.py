"""Pytest fixtures —— 每个测试用临时 DATA_DIR + MUSIC_ROOT，互不污染."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def env(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    music_root = tmp_path / "music"
    data_dir = tmp_path / "data"
    music_root.mkdir()
    data_dir.mkdir()
    monkeypatch.setenv("MUSIC_ROOT", str(music_root))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("BIND_PORT", "8000")
    # 显式覆盖 .env 里可能开了的写入开关
    monkeypatch.setenv("ALLOW_FILE_WRITES", "false")
    monkeypatch.setenv("ACOUSTID_API_KEY", "")

    # reset 全局单例（test 间互不污染）
    from app import beets_bridge, config, db
    from app.workers import archive_extract, cue_split, musicbrainz

    config._settings = None
    db._engine = None
    db._SessionLocal = None
    beets_bridge._reset_for_tests()
    musicbrainz._reset_for_tests()
    cue_split._reset_for_tests()
    archive_extract._reset_for_tests()

    # 先建 our_db 的 schema
    from app.db import run_migrations

    run_migrations()

    return {"music_root": music_root, "data_dir": data_dir}


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
