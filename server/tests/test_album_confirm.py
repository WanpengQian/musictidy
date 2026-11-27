"""第 3 层打分 (离线): 时长对位 / 部分碟 / 近似 TOC / 专辑名策略."""

from __future__ import annotations

from app.album_confirm import apply_confirmation, approx_toc, score_release
from app.folder_parse import ParsedAlbum, ParsedTrack


def _album(durs: dict[tuple[int, int], float], votes=None) -> ParsedAlbum:
    tracks = [ParsedTrack(path=f"/x/{d}-{n}.flac", disc=d, track=n, title=f"t{n}",
                          title_source="tag", duration=v) for (d, n), v in durs.items()]
    return ParsedAlbum(dir="/x", kind="album", artist="A", album="Bad Boy", year="",
                       confidence="medium", tracks=tracks, artist_votes=[],
                       album_votes=votes or [], notes=[])


def _rel(media: dict[int, list[float]]) -> dict:
    return {"id": "r1", "title": "R", "rg_id": "g1", "artists": [],
            "media": [{"position": p, "tracks": [
                {"position": i + 1, "title": f"t{i + 1}", "recording_mbid": f"rec{p}{i}",
                 "length_s": v} for i, v in enumerate(ls)]} for p, ls in media.items()]}


def test_score_full_match_with_tolerance():
    a = _album({(1, 1): 200.0, (1, 2): 301.5, (1, 3): 250.0})
    sc = score_release(a, _rel({1: [201.0, 300.0, 249.0]}))
    assert sc["score"] == 1.0


def test_score_penalises_extra_release_tracks_and_bad_durations():
    a = _album({(1, 1): 200.0, (1, 2): 300.0})
    assert score_release(a, _rel({1: [200.0, 300.0, 250.0, 260.0]}))["score"] == 0.5
    assert score_release(a, _rel({1: [200.0, 330.0]}))["score"] == 0.5


def test_score_partial_discs_only_compares_present_discs():
    a = _album({(1, 1): 100.0, (1, 2): 110.0, (3, 1): 120.0})
    sc = score_release(a, _rel({1: [100.0, 110.0], 2: [99.0], 3: [120.0]}))
    assert sc["score"] == 1.0 and sc["release_discs"] == 3 and sc["folder_discs"] == 2


def test_approx_toc_requires_contiguous_numbers():
    a = _album({(1, 1): 2.0, (1, 2): 4.0})
    assert approx_toc(a) == "1 2 600 150 300"
    assert approx_toc(_album({(1, 1): 2.0, (1, 3): 4.0})) is None


def test_apply_keeps_solid_local_album_name():
    votes = [{"value": "BAD BOY", "weight": 8.5, "sources": ["tag", "CUE", "目录名"]}]
    a = _album({(1, 1): 200.0}, votes)
    res = {"status": "confirmed", "method": "toc", "score": 1.0, "title": "張惠妹 II",
           "date": "1997-06-01", "rg_mbid": "g", "release_mbid": "r", "tracks": [],
           "match": {"matched": 1, "folder_tracks": 1, "folder_discs": 1, "release_discs": 1}}
    apply_confirmation(a, res)
    assert a.album == "Bad Boy" and a.year == "1997" and a.confidence == "high"


def test_apply_uses_mb_name_when_local_weak():
    votes = [{"value": "牵手", "weight": 2.5, "sources": ["目录名"]},
             {"value": "Holding Hands", "weight": 2.0, "sources": ["抓轨日志"]}]
    a = _album({(1, 1): 200.0}, votes)
    a.album = "牵手x"
    res = {"status": "confirmed", "method": "title", "score": 1.0, "title": "牽手",
           "date": "1998", "rg_mbid": "g", "release_mbid": "r", "tracks": [],
           "match": {"matched": 1, "folder_tracks": 1, "folder_discs": 1, "release_discs": 1}}
    apply_confirmation(a, res)
    assert a.album == "牽手"
