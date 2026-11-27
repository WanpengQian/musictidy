"""收件箱端到端: 真 flac (ffmpeg 生成) → 本地解析提议 → 一键批准 → Library + beets + 别名."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")


def _flac(path: Path, secs: float, **tags: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={secs}", *meta, str(path)],
        check=True,
    )


@pytest.fixture
def inbox_env(env, monkeypatch, tmp_path):
    src = tmp_path / "inbox"
    src.mkdir()
    monkeypatch.setenv("SOURCE_ROOTS", str(src))
    monkeypatch.setenv("ALLOW_FILE_WRITES", "true")
    from app import artist_alias, config

    config._settings = None
    artist_alias._invalidate()
    return {**env, "src": src}


def test_parse_first_proposal_and_batch_approve(inbox_env):
    from app import artist_alias, beets_bridge, inbox
    from app.config import get_settings

    artist_alias.learn(["Amei"], "張惠妹", "aeacef89-0000-0000-0000-000000000000")
    d = inbox_env["src"] / "Amei - 勇敢" / "CD2"
    _flac(d / "01. x.flac", 1.0, title="[捌零音乐论坛]看见自己", artist="张惠妹", album="勇敢", track="1")
    _flac(d / "02. y.flac", 1.2, title="假惺惺", artist="张惠妹", album="勇敢", track="2")
    _flac(d / "02. y.mp3", 1.2, title="假惺惺", artist="张惠妹", album="勇敢", track="2")  # 同轨低格式

    props = inbox.compute_proposals()
    [f] = props["folders"]
    assert f["parsed"]["artist"] == "張惠妹" and f["parsed"]["album"] == "勇敢"
    assert f["parsed"]["confidence"] == "high"
    assert [(s["disc"], s["track"], s["title"], s["fmt"]) for s in f["songs"]] == [
        (2, 1, "看见自己", "flac"), (2, 2, "假惺惺", "flac")]
    assert f["candidates"][0]["source"] == "local"
    assert all(c["source"] == "local" for c in f["candidates"])   # 高置信不混指纹候选

    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    res = inbox.approve_all_high(lib)
    assert res["succeeded"] == 1 and res["copied"] == 2, res
    out = sorted(p.relative_to(s.music_root).as_posix() for p in s.music_root.rglob("*.flac"))
    assert out == ["張惠妹/勇敢/Disc 2/01. 看见自己.flac", "張惠妹/勇敢/Disc 2/02. 假惺惺.flac"]
    items = list(lib.items())
    assert {it.albumartist for it in items} == {"張惠妹"}
    assert {it.mb_albumartistid for it in items} == {"aeacef89-0000-0000-0000-000000000000"}

    # 源文件不动 + 再扫一遍 = 已整理, 批量批准不重复拷
    assert (d / "01. x.flac").exists()
    [f2] = inbox.compute_proposals()["folders"]
    assert f2["organized"]
    assert inbox.approve_all_high(lib)["albums"] == 0


def test_manual_approve_learns_alias(inbox_env):
    from app import artist_alias, beets_bridge, inbox
    from app.config import get_settings

    d = inbox_env["src"] / "牵手"
    _flac(d / "01.flac", 1.0)
    props = inbox.compute_proposals()
    [f] = props["folders"]
    assert f["parsed"]["confidence"] == "low"          # 没 tag 没歌手

    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    r = inbox.approve("", "牵手", "张惠妹", [f["songs"][0]["src"]], lib,
                      tracks=[{"src": f["songs"][0]["src"], "disc": 1, "track": 1, "title": "Are You Ready"}],
                      artist_aliases=["A-mei Cheung"])
    assert r["ok"] and r["copied"] == 1
    assert (s.music_root / "张惠妹" / "牵手" / "01. Are You Ready.flac").exists()
    assert artist_alias.index().canonical("A-mei Cheung") == "张惠妹"


def test_cleanup_verifies_then_trash_and_restore(inbox_env, monkeypatch):
    from app import beets_bridge, inbox, inbox_cleanup
    from app.config import get_settings

    monkeypatch.setattr(inbox_cleanup, "SETTLE_SECONDS", 0)
    src = inbox_env["src"]
    d = src / "张惠妹 - 勇敢"
    _flac(d / "01. a.flac", 1.0, title="看见自己", artist="张惠妹", album="勇敢", track="1")
    _flac(d / "02. b.flac", 1.2, title="假惺惺", artist="张惠妹", album="勇敢", track="2")
    _flac(d / "mp3" / "02. b.mp3", 1.2, title="假惺惺", artist="张惠妹", album="勇敢", track="2")
    (d / "cover.jpg").write_bytes(b"x")
    _flac(src / "别的" / "01. x.flac", 1.0, title="x", artist="某人", album="别的", track="1")

    rep = {e["name"]: e for e in inbox_cleanup.cleanup_report()["entries"]}
    assert rep["张惠妹 - 勇敢"]["status"] == "pending" and rep["张惠妹 - 勇敢"]["blocker_count"] == 3

    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    [f] = [f for f in inbox.compute_proposals()["folders"] if "勇敢" in f["folder"]]
    songs = f["songs"]
    r = inbox.approve("", "勇敢", "张惠妹", [x["src"] for x in songs], lib,
                      tracks=[{"src": x["src"], "disc": x["disc"], "track": x["track"],
                               "title": x["title"]} for x in songs])
    assert r["copied"] == 2                                      # mp3 被 flac 顶替, 不拷

    rep = {e["name"]: e for e in inbox_cleanup.cleanup_report()["entries"]}
    assert rep["张惠妹 - 勇敢"]["status"] == "deletable"          # mp3 也算有着落
    assert rep["张惠妹 - 勇敢"]["accounted"] == 3
    assert rep["别的"]["status"] == "pending"

    res = inbox_cleanup.trash_entries([str(d), str(src / "别的")])
    assert [m["path"] for m in res["moved"]] == [str(d)]
    assert res["refused"][0]["path"] == str(src / "别的")        # 没入库的拒绝
    assert not d.exists() and (src / "别的").exists()

    [item] = inbox_cleanup.list_trash()
    assert inbox_cleanup.restore(item["id"])["ok"]
    assert (d / "01. a.flac").exists() and inbox_cleanup.list_trash() == []


def _mp3(path: Path, secs: float, **tags: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=330:duration={secs}", *meta, "-c:a", "libmp3lame", str(path)],
        check=True,
    )


def _approve_folder_by_name(inbox, lib, name: str, **kw):
    [f] = [f for f in inbox.compute_proposals()["folders"] if name in f["folder"]]
    songs = [x for x in f["songs"] if not x["covered"]]
    c = f["candidates"][0]
    return f, inbox.approve(
        c["rg_mbid"], c["album"], c["album_artist"], [x["src"] for x in songs], lib,
        tracks=[{"src": x["src"], "disc": x["disc"], "track": x["track"], "title": x["title"]}
                for x in songs], **kw)


def test_reorganize_same_album_different_files(inbox_env, monkeypatch):
    from app import beets_bridge, inbox, inbox_cleanup
    from app.config import get_settings

    monkeypatch.setattr(inbox_cleanup, "SETTLE_SECONDS", 0)
    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    src = inbox_env["src"]
    tags = {"artist": "张惠妹", "album": "勇敢"}

    # 第一批: mp3 版, 只有第 1 首 → 入库
    _mp3(src / "张惠妹 - 勇敢 (mp3)" / "01. a.mp3", 1.0, title="看见自己", track="1", **tags)
    _, r = _approve_folder_by_name(inbox, lib, "(mp3)")
    assert r["copied"] == 1

    # 第二批: flac 版 (更好) 1-2 首 + 同一首 flac 改过 tag 的另一份
    d = src / "张惠妹 - 勇敢 (flac)"
    _flac(d / "01. a.flac", 1.0, title="看见自己", track="1", **tags)
    _flac(d / "02. b.flac", 1.2, title="假惺惺", track="2", **tags)
    f, _ = None, None
    [f] = [f for f in inbox.compute_proposals()["folders"] if "(flac)" in f["folder"]]
    assert f["library_match"]["album"] == "勇敢"
    assert {x["track"]: x["lib_status"] for x in f["songs"]} == {1: "upgrade", 2: "new"}
    assert f["needs_decision"]
    assert inbox.approve_all_high(lib)["albums"] == 0            # 要替换的不自动批

    # 先不替换: 只补第 2 首, 拷进同一张专辑目录
    _, r = _approve_folder_by_name(inbox, lib, "(flac)")
    assert r["copied"] == 1 and r["into_existing"]
    album_dir = s.music_root / "张惠妹" / "勇敢"
    assert sorted(p.name for p in album_dir.iterdir()) == ["01. 看见自己.mp3", "02. 假惺惺.flac"]

    # 再选替换: mp3 进回收站, flac 顶上
    _, r = _approve_folder_by_name(inbox, lib, "(flac)", upgrade_mode="replace")
    assert r["replaced"] == 1
    assert sorted(p.name for p in album_dir.iterdir()) == ["01. 看见自己.flac", "02. 假惺惺.flac"]
    assert len(list(lib.items())) == 2
    assert list((s.trash_dir / "library").glob("*看见自己.mp3"))

    # 同一张又来一份 (改过 tag, md5 不同) → 库里已有, 可清理
    _flac(src / "勇敢 重复" / "01. a.flac", 1.0, title="看见自己", track="1",
          comment="retagged", artist="张惠妹", album="勇敢")
    [f3] = [f for f in inbox.compute_proposals()["folders"] if "重复" in f["folder"]]
    assert f3["songs"][0]["lib_status"] == "have" and f3["organized"]
    rep = {e["name"]: e["status"] for e in inbox_cleanup.cleanup_report()["entries"]}
    assert rep["勇敢 重复"] == "deletable"
    assert rep["张惠妹 - 勇敢 (mp3)"] == "deletable"     # 旧 mp3 源: md5 记在库内容里


def test_keep_library_version_marks_accounted(inbox_env, monkeypatch):
    from app import beets_bridge, inbox, inbox_cleanup
    from app.config import get_settings

    monkeypatch.setattr(inbox_cleanup, "SETTLE_SECONDS", 0)
    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    src = inbox_env["src"]
    tags = {"artist": "张惠妹", "album": "姊妹", "title": "姐妹", "track": "1"}
    _mp3(src / "姊妹 mp3" / "01. a.mp3", 1.0, **tags)
    _approve_folder_by_name(inbox, lib, "mp3")
    _flac(src / "姊妹 flac" / "01. a.flac", 1.0, **tags)

    rep = {e["name"]: e["status"] for e in inbox_cleanup.cleanup_report()["entries"]}
    assert rep["姊妹 flac"] == "pending"
    _, r = _approve_folder_by_name(inbox, lib, "flac", upgrade_mode="keep")
    assert r["kept"] == 1 and r["copied"] == 0
    [f] = [f for f in inbox.compute_proposals()["folders"] if "flac" in f["folder"]]
    assert f["songs"][0]["lib_status"] == "kept" and f["organized"]
    rep = {e["name"]: e["status"] for e in inbox_cleanup.cleanup_report()["entries"]}
    assert rep["姊妹 flac"] == "deletable"
