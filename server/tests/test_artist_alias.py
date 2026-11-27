"""歌手别名表 (第 2 层) —— 解析 / 学习 / 优先级 / 接入 folder_parse 投票."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def env(monkeypatch, tmp_path: Path):
    """轻量: 只要一个空 musictidy.db (别名表自建), 不跑全量迁移。"""
    (tmp_path / "music").mkdir()
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path / "music"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import artist_alias, config, db

    config._settings = None
    db._engine = None
    db._SessionLocal = None
    artist_alias._invalidate()
    yield
    artist_alias._invalidate()


def test_learn_resolve_and_key(env):
    from app import artist_alias as A

    A.learn(["Amei", "A-mei Cheung", "张惠妹"], "張惠妹", "aeacef89-0000-0000-0000-000000000000")
    idx = A.index()
    assert idx.resolve("amei") == ("張惠妹", "aeacef89-0000-0000-0000-000000000000")
    assert idx.resolve("A-mei II")[0] == "張惠妹"          # 尾部罗马数字 (第二张专辑)
    assert idx.key("Amei") == idx.key("張惠妹") == idx.key("A-mei Cheung")
    assert idx.key("未知歌手") == "未知歌手"


def test_lower_priority_source_cannot_rebind(env):
    from app import artist_alias as A
    from app.db import get_engine

    A.learn(["Beyond"], "Beyond", "11111111-0000-0000-0000-000000000000", source="user")
    with get_engine().begin() as conn:
        A._ensure_table(conn)
        assert not A._upsert(conn, "Beyond", "Beyond (UK)", "22222222-0000-0000-0000-000000000000", "mb")
    A._invalidate()
    assert A.index().resolve("beyond")[1].startswith("1111")


def test_set_canonical_renames_whole_group(env):
    from app import artist_alias as A

    A.learn(["张惠妹", "Amei"], "張惠妹", "aeacef89-0000-0000-0000-000000000000")
    A.set_canonical("aeacef89-0000-0000-0000-000000000000", "张惠妹")
    idx = A.index()
    assert idx.canonical("Amei") == "张惠妹"
    assert idx.resolve("张惠妹")[1].startswith("aeacef89")


def test_parse_folder_merges_votes_through_alias(env, tmp_path):
    from app import artist_alias as A
    from app.folder_parse import parse_folder

    A.learn(["Amei"], "張惠妹", "aeacef89-0000-0000-0000-000000000000")
    idx = A.index()
    d = tmp_path / "src" / "Amei - 勇敢"
    d.mkdir(parents=True)
    files = []
    for n in (1, 2):
        f = d / f"0{n}. x{n}.flac"
        f.write_bytes(b"")
        files.append(f)
    (d / "a.cue").write_text('PERFORMER "張惠妹"\nTITLE "勇敢"\n', encoding="utf-8")
    [a] = parse_folder(d, files, tmp_path / "src", artist_key=idx.key, artist_canon=idx.canonical)
    assert a.artist == "張惠妹"
    assert len(a.artist_votes) == 1                     # 目录名 Amei + CUE 張惠妹 聚成一票
    assert a.confidence == "high"
