"""Archive 解压器 + worker 测试."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import archive


def _touch(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def test_detect_archives_finds_supported_exts(tmp_path):
    _touch(tmp_path / "a.zip")
    _touch(tmp_path / "sub" / "b.rar")
    _touch(tmp_path / "deep" / "nested" / "c.7z")
    _touch(tmp_path / "not_an_archive.txt")
    found = {p.name for p in archive.detect_archives(tmp_path)}
    assert found == {"a.zip", "b.rar", "c.7z"}


def test_detect_archives_skips_extracted_and_hidden(tmp_path):
    _touch(tmp_path / "_extracted" / "old.zip")
    _touch(tmp_path / ".trash" / "hidden.zip")
    _touch(tmp_path / "good.zip")
    found = {p.name for p in archive.detect_archives(tmp_path)}
    assert found == {"good.zip"}


def test_is_already_extracted(tmp_path):
    arc = tmp_path / "album.zip"
    _touch(arc)
    assert not archive.is_already_extracted(arc)

    # 创个解压目录但空 = 没解
    (tmp_path / "_extracted" / "album").mkdir(parents=True)
    assert not archive.is_already_extracted(arc)

    # 加文件 = 已解
    _touch(tmp_path / "_extracted" / "album" / "01.flac")
    assert archive.is_already_extracted(arc)


def test_extraction_dst(tmp_path):
    arc = tmp_path / "sub" / "Hello.zip"
    dst = archive.extraction_dst(arc)
    assert dst == tmp_path / "sub" / "_extracted" / "Hello"


@pytest.mark.asyncio
async def test_handler_skip_when_writes_disabled(env, monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOW_FILE_WRITES", "false")
    from app import config
    config._settings = None
    from app.workers import archive_extract
    archive_extract._reset_for_tests()

    arc = tmp_path / "x.zip"
    _touch(arc)
    await archive_extract.handle_archive_extract({"archive": str(arc)})
    assert arc.exists()  # 不动


@pytest.mark.asyncio
async def test_handler_pre_extracted_moves_to_trash(env, monkeypatch, tmp_path):
    """已解过的档案直接移 trash，不重解."""
    monkeypatch.setenv("ALLOW_FILE_WRITES", "true")
    monkeypatch.setenv("DATA_DIR", str(env["data_dir"]))
    from app import config
    config._settings = None
    from app.workers import archive_extract
    archive_extract._reset_for_tests()

    arc = env["music_root"] / "album.zip"
    _touch(arc)
    # 模拟已解
    extracted_file = env["music_root"] / "_extracted" / "album" / "01.flac"
    _touch(extracted_file)

    extract_called = []
    monkeypatch.setattr(archive, "extract",
                        lambda a: extract_called.append(a) or a.parent / "_extracted" / a.stem)

    await archive_extract.handle_archive_extract({"archive": str(arc)})

    assert not arc.exists()  # 已进 trash
    assert extracted_file.exists()  # 解压内容仍在
    assert extract_called == []  # 没真调 unar
