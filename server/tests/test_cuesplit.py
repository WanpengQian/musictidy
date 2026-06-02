"""CUE parser + worker tests. Uses small synthetic CUEs (no ffmpeg invoked in unit tests)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import cuesplit


SIMPLE_CUE_UTF8 = """REM GENRE JPop
REM DATE 2008
PERFORMER "いきものがかり"
TITLE "ライフアルバム"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Good Morning"
    PERFORMER "いきものがかり"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "茜色の約束"
    INDEX 01 04:42:50
  TRACK 03 AUDIO
    TITLE "夏空グラフィティ"
    INDEX 01 09:12:30
"""


def test_parse_utf8(tmp_path):
    cue = tmp_path / "album.cue"
    cue.write_text(SIMPLE_CUE_UTF8, encoding="utf-8")
    sheet = cuesplit.parse_cue(cue)

    assert sheet.title == "ライフアルバム"
    assert sheet.performer == "いきものがかり"
    assert sheet.file == "album.flac"
    assert len(sheet.tracks) == 3
    assert sheet.tracks[0].title == "Good Morning"
    assert sheet.tracks[0].start_seconds == 0.0
    assert sheet.tracks[1].title == "茜色の約束"
    # MM:SS:FF = 04:42:50 → 4*60 + 42 + 50/75
    assert abs(sheet.tracks[1].start_seconds - (4 * 60 + 42 + 50 / 75)) < 0.001
    # end_seconds 应该被推断
    assert sheet.tracks[0].end_seconds == sheet.tracks[1].start_seconds
    assert sheet.tracks[2].end_seconds is None  # 最后一首


def test_parse_shift_jis(tmp_path):
    cue = tmp_path / "album.cue"
    cue.write_bytes(SIMPLE_CUE_UTF8.encode("shift_jis", errors="replace"))
    sheet = cuesplit.parse_cue(cue)
    # 至少结构能解出来，文字可能因为 shift_jis 编码不全部正确
    assert len(sheet.tracks) == 3
    assert sheet.tracks[0].title in ("Good Morning",)  # ASCII 必正确


def test_parse_gb18030_chinese(tmp_path):
    chinese = """PERFORMER "女子十二乐坊"
TITLE "眉飞色舞"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "自由"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "刘三姐"
    INDEX 01 03:30:00
"""
    cue = tmp_path / "album.cue"
    cue.write_bytes(chinese.encode("gb18030"))
    sheet = cuesplit.parse_cue(cue)
    assert sheet.performer == "女子十二乐坊"
    assert sheet.title == "眉飞色舞"
    assert sheet.tracks[0].title == "自由"


def test_detect_pairs_basic(tmp_path):
    (tmp_path / "album.cue").write_text(SIMPLE_CUE_UTF8, encoding="utf-8")
    # 用大点的"假"flac，避免大小为 0 被过滤
    (tmp_path / "album.flac").write_bytes(b"x" * 1024)
    pairs = cuesplit.detect_pairs(tmp_path)
    assert len(pairs) == 1
    assert pairs[0][0].name == "album.cue"
    assert pairs[0][1].name == "album.flac"


def test_detect_pairs_skips_already_split(tmp_path):
    (tmp_path / "album.cue").write_text(SIMPLE_CUE_UTF8, encoding="utf-8")
    (tmp_path / "album.flac").write_bytes(b"x" * 1024)
    # 模拟已经切过 —— 同目录多个 flac
    for i in range(1, 5):
        (tmp_path / f"{i:02d}. track.flac").write_bytes(b"x" * 100)
    pairs = cuesplit.detect_pairs(tmp_path)
    assert pairs == []


def test_detect_pairs_falls_back_when_filename_wrong(tmp_path):
    cue_text = """PERFORMER "X"
TITLE "Y"
FILE "wrong_name.flac" WAVE
  TRACK 01 AUDIO
    TITLE "T1"
    INDEX 01 00:00:00
"""
    (tmp_path / "album.cue").write_text(cue_text, encoding="utf-8")
    # 文件名跟 CUE 不匹配，但目录里只有一个 flac → 应回退取它
    (tmp_path / "actual.flac").write_bytes(b"x" * 1024)
    pairs = cuesplit.detect_pairs(tmp_path)
    assert len(pairs) == 1
    assert pairs[0][1].name == "actual.flac"


@pytest.mark.asyncio
async def test_handler_skips_when_writes_disabled(env, monkeypatch):
    """ALLOW_FILE_WRITES=false → handler 静默完成不重试."""
    monkeypatch.setenv("ALLOW_FILE_WRITES", "false")
    from app import config
    config._settings = None
    from app.workers import cue_split
    cue_split._reset_for_tests()

    # 即使路径不存在也不应该抛
    await cue_split.handle_cue_split({
        "cue": "/nonexistent/album.cue",
        "src_audio": "/nonexistent/album.flac",
    })


@pytest.mark.asyncio
async def test_handler_splits_and_trashes_originals(env, monkeypatch, tmp_path):
    """端到端 mock：split_pair + beets 接口都 mock，验证调用次序."""
    monkeypatch.setenv("ALLOW_FILE_WRITES", "true")
    from app import config
    config._settings = None

    music_root = env["music_root"]
    cue = music_root / "album.cue"
    src = music_root / "album.flac"
    cue.write_text("dummy", encoding="utf-8")
    src.write_bytes(b"audio")

    # mock split: 返回 3 个假 dst
    new_files = [music_root / f"{i:02d}. Track {i}.flac" for i in (1, 2, 3)]
    for p in new_files:
        p.write_bytes(b"track")

    from app import cuesplit as _cs
    from app.workers import cue_split as worker
    monkeypatch.setattr(_cs, "split_pair", lambda *_a, **_kw: new_files)

    # mock beets_bridge
    class FakeLib:
        directory = str(music_root).encode()
        def items(self): return []
    monkeypatch.setattr(worker.beets_bridge, "get_library", lambda *_a, **_kw: FakeLib())

    imported = []
    def fake_import(lib, p):
        imported.append(p)
        return 100 + len(imported)
    monkeypatch.setattr(worker.beets_bridge, "import_file", fake_import)

    await worker.handle_cue_split({"cue": str(cue), "src_audio": str(src)})

    # 原文件应该被 mv 到 trash
    assert not cue.exists()
    assert not src.exists()
    # 新文件应该被 import
    assert len(imported) == 3
    # fingerprint 任务应该排了
    from app.workers import queue
    assert queue.counts_by_status()["queued"] == 3
