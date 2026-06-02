"""Scan worker —— 走 fs walk 但 mock 掉 beets，避免依赖真实音频文件."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workers import queue, scan


def _touch(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")


def test_walk_audio_files_finds_extensions(env):
    root: Path = env["music_root"]
    _touch(root / "a.flac")
    _touch(root / "sub" / "b.ape")
    _touch(root / "c.txt")          # 应被过滤
    _touch(root / ".hidden" / "d.flac")  # 隐藏目录应被过滤

    found = {p.name for p in scan.walk_audio_files(root)}
    assert found == {"a.flac", "b.ape"}


@pytest.mark.asyncio
async def test_scan_and_import_with_mocked_beets(env, monkeypatch):
    root: Path = env["music_root"]
    _touch(root / "one.flac")
    _touch(root / "two.flac")

    # mock 掉 beets API：不真的读 tag，假装每次 import 成功
    fake_known: set[Path] = set()
    fake_added: list[Path] = []

    class FakeLib:
        pass

    def fake_get_library(db_path, music_root):
        return FakeLib()

    def fake_all_known_paths(lib):
        return set(fake_known)

    def fake_count_items(lib):
        return len(fake_known)

    def fake_count_identified(lib):
        return 0

    counter = {"next_id": 100}

    def fake_import_file(lib, path: Path):
        fake_known.add(path)
        fake_added.append(path)
        counter["next_id"] += 1
        return counter["next_id"]

    monkeypatch.setattr(scan.beets_bridge, "get_library", fake_get_library)
    monkeypatch.setattr(scan.beets_bridge, "all_known_paths", fake_all_known_paths)
    monkeypatch.setattr(scan.beets_bridge, "count_items", fake_count_items)
    monkeypatch.setattr(scan.beets_bridge, "count_identified", fake_count_identified)
    monkeypatch.setattr(scan.beets_bridge, "import_file", fake_import_file)

    result = await scan.scan_and_import()
    assert result["scanned"] == 2
    assert result["added"] == 2
    assert result["skipped"] == 0
    assert result["failed"] == 0

    # 第二次应该全 skip
    result2 = await scan.scan_and_import()
    assert result2["scanned"] == 2
    assert result2["added"] == 0
    assert result2["skipped"] == 2

    # 每个新 item 排了一个 fingerprint 任务
    assert queue.counts_by_status()["queued"] == 2
