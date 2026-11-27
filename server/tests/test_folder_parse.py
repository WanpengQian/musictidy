"""本地结构解析 (folder_parse) —— 目录名 / 文件名 / 乱码 / 水印 / CUE / 日志 / 投票."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.folder_parse import (
    clean_value,
    disc_from_path,
    fix_mojibake,
    norm_key,
    parse_dir_name,
    parse_filename,
    parse_folder,
    parse_info_text,
    parse_rip_log,
    strip_watermark,
)


# ── 归一化 / 乱码 / 水印 ─────────────────────────────────────────
def test_norm_key_simplified_traditional_case_punct():
    assert norm_key("張惠妹") == norm_key("张惠妹")
    assert norm_key("動力火車") == norm_key("动力火车")
    assert norm_key("A-mei") == norm_key("Amei")
    assert norm_key("BAD BOY") == norm_key("Bad Boy")


def test_fix_mojibake_gbk_read_as_latin1():
    assert fix_mojibake("ÕÅ»ÝÃÃ") == ("张惠妹", True)
    assert fix_mojibake("°®ÊÇÎ¨Ò»")[0] == "爱是唯一"


def test_fix_mojibake_leaves_good_text():
    for s in ("张惠妹", "Céline Dion", "Björk", "中島美嘉", "BAD BOY"):
        assert fix_mojibake(s) == (s, False)


def test_fix_mojibake_rejects_truncated_bytes():
    # 原始字节丢了一个, 硬解会冒生僻字 → 宁可不还原
    fixed, changed = fix_mojibake("Ò²Ðúß÷Ì\x81E")
    assert not changed


def test_strip_watermark():
    assert strip_watermark("[捌零音乐论坛]张惠妹") == "张惠妹"
    assert strip_watermark("勇敢 (www.foo.com)") == "勇敢"
    assert strip_watermark("听海 (Live)") == "听海 (Live)"


def test_clean_value_drops_unfixable_garbage():
    assert clean_value("張瞞cf")[0] == ""
    assert clean_value("2o?a")[0] == ""
    assert clean_value("[捌零音乐论坛]勇敢")[0] == "勇敢"


# ── 目录名 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("name,artist,album,year", [
    ("张惠妹-勇敢", "张惠妹", "勇敢", ""),
    ("Amei - 我可以抱你吗", "Amei", "我可以抱你吗", ""),
    ("A-mei II- Bad Boy", "A-mei II", "Bad Boy", ""),
    ("[APE][正]张惠妹 - 勇敢", "张惠妹", "勇敢", ""),
    ("张惠妹 - 勇敢 (2003) [FLAC]", "张惠妹", "勇敢", "2003"),
    ("[张惠妹] 勇敢", "张惠妹", "勇敢", ""),
    ("张惠妹《勇敢》", "张惠妹", "勇敢", ""),
    ("2003 - 勇敢", "", "", "2003"),
    ("张惠妹 - 2003 - 勇敢", "张惠妹", "勇敢", "2003"),
    ("中島美嘉 - LØVE [24bit 96kHz]", "中島美嘉", "LØVE", ""),
])
def test_parse_dir_name(name, artist, album, year):
    d = parse_dir_name(name)
    assert (d.artist, d.album, d.year) == (artist, album, year)


def test_parse_dir_name_single_segment():
    assert parse_dir_name("也许明天").whole == "也许明天"
    assert parse_dir_name("A-mei").whole == "A-mei"          # 裸连字符两边无 CJK/空格 → 不切
    assert parse_dir_name("2003 - 勇敢").whole == "勇敢"
    assert parse_dir_name("勇敢 FLAC").whole == "勇敢"


# ── 文件名 / 碟号 ───────────────────────────────────────────────
@pytest.mark.parametrize("stem,expected", [
    ("01. 看见自己", (0, 1, "看见自己")),
    ("01-看见自己", (0, 1, "看见自己")),
    ("01 看见自己", (0, 1, "看见自己")),
    ("1-13 有谁共鸣", (1, 13, "有谁共鸣")),
    ("1-01 張國榮 - 風繼續吹", (1, 1, "張國榮 - 風繼續吹")),
    ("Track 05", (0, 5, "")),
    ("01", (0, 1, "")),
    ("看见自己", (0, 0, "看见自己")),
])
def test_parse_filename(stem, expected):
    assert parse_filename(stem) == expected


def test_disc_from_path():
    a = Path("/m/最红")
    assert disc_from_path(a / "CD2" / "01.mp3", a) == 2
    assert disc_from_path(a / "最红 CD3_tracks" / "01.flac", a) == 3
    assert disc_from_path(a / "Disc 1" / "01.flac", a) == 1
    assert disc_from_path(a / "01.flac", a) == 0


# ── CUE / 日志 / 说明文件 ───────────────────────────────────────
def _touch(p: Path, data: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_rip_log_bilingual_header_and_track_names(tmp_path):
    log = (
        "EAC extraction logfile from 7. March 2003, 23:01 for CD\r\n"
        "張瞞cf - A-mei Cheung / 2o?a - Holding Hands\r\n\r\n"
        "Track  1\r\n   Filename C:\\My Music\\1-1 Are You Ready.wav\r\n\r\n"
        "Track  2\r\n   Filename C:\\My Music\\1-2 不要騙我.wav\r\n"
    ).encode("big5")
    r = parse_rip_log(_touch(tmp_path / "x.LOG", log))
    assert r.artist == ["A-mei Cheung"]
    assert r.album == ["Holding Hands"]
    assert r.tracks == {1: "Are You Ready", 2: "不要騙我"}


def test_rip_log_empty_header_and_bare_numbers(tmp_path):
    log = ("EAC extraction logfile from 17. March 2004\r\n / \r\n\r\n"
           "Track  1\r\n   Filename F:\\ - 01.wav\r\n").encode("gbk")
    r = parse_rip_log(_touch(tmp_path / "x.LOG", log))
    assert r.artist == [] and r.album == [] and r.tracks == {}


def test_info_text(tmp_path):
    txt = "专辑名称:勇敢\r\n发行公司:华纳唱片\r\n发行时间:2003年06月27日\r\n".encode("gbk")
    assert parse_info_text(_touch(tmp_path / "intro.txt", txt)) == {"album": "勇敢", "year": "2003-06-27"}


# ── 整目录 ──────────────────────────────────────────────────────
def test_folder_loose_cue_dir_and_info_agree(tmp_path):
    d = tmp_path / "VER2" / "[APE][正]张惠妹 - 勇敢"
    files = [_touch(d / f"0{i}-歌{i}.ape") for i in (1, 2)]
    cue = ('PERFORMER 张惠妹\r\nTITLE 勇敢\r\nFILE "01-歌1.ape" WAVE\r\n'
           "  TRACK 01-歌1 AUDIO\r\n    TITLE 看见自己\r\n    INDEX 01 00:00:00\r\n"
           "  TRACK 02-歌2 AUDIO\r\n    TITLE 假惺惺\r\n    INDEX 01 04:00:00\r\n")
    _touch(d / "勇敢.CUE", cue.encode("gbk"))
    [a] = parse_folder(d, files, tmp_path)
    assert (a.artist, a.album) == ("张惠妹", "勇敢")
    assert [t.title for t in a.tracks] == ["看见自己", "假惺惺"]
    assert a.confidence == "high"
    assert all("VER2" not in v["value"] for v in a.artist_votes)   # 版本分组目录不当歌手


def test_folder_titles_from_log_when_no_tags(tmp_path):
    d = tmp_path / "牵手"
    files = [_touch(d / "01.ape"), _touch(d / "02.ape")]
    log = ("EAC extraction logfile from 2003 for CD\r\nA-mei / Holding Hands\r\n\r\n"
           "Track  1\r\n   Filename C:\\1-1 Are You Ready.wav\r\n\r\n"
           "Track  2\r\n   Filename C:\\1-2 不要騙我.wav\r\n").encode("big5")
    _touch(d / "x.LOG", log)
    [a] = parse_folder(d, files, tmp_path)
    assert [(t.track, t.title, t.title_source) for t in a.tracks] == [
        (1, "Are You Ready", "log"), (2, "不要騙我", "log")]
    assert a.artist == "A-mei"


def test_folder_multi_disc_tracks_dirs(tmp_path):
    d = tmp_path / "最红"
    files = [_touch(d / f"最红 CD{c}_tracks" / f"0{n}. 歌{c}{n}.flac") for c in (1, 2) for n in (1, 2)]
    [a] = parse_folder(d, files, tmp_path)
    assert {(t.disc, t.track) for t in a.tracks} == {(1, 1), (1, 2), (2, 1), (2, 2)}


def test_folder_missing_track_numbers_downgrade(tmp_path):
    d = tmp_path / "张惠妹 - 勇敢"
    files = [_touch(d / "01. a.flac"), _touch(d / "03. c.flac")]
    [a] = parse_folder(d, files, tmp_path)
    assert a.confidence != "high"
    assert any("缺轨号" in n for n in a.notes)
