"""年份来源 (离线): 日期归一 / 默认挑选 / 维基消歧义后缀 / Wikidata 精度."""

from __future__ import annotations

from app import album_dates as D
from app.folder_parse import norm_date


def test_norm_date_formats():
    assert norm_date("2003年06月27日") == "2003-06-27"
    assert norm_date("1998/10/12") == "1998-10-12"
    assert norm_date("2003.6") == "2003-06"
    assert norm_date("2004") == "2004"
    assert norm_date("发行: 未知") == ""


def test_pick_default_prefers_mb_first_release_then_precision():
    dates = [
        {"date": "2004", "source": "tag"},
        {"date": "2012-01-01", "source": D.SRC_MB_RELEASE},     # 再版, 垫底
        {"date": "1999-06-08", "source": D.SRC_WIKIDATA},
        {"date": "1999-06-07", "source": D.SRC_MB_FIRST},
    ]
    assert D.pick_default(dates) == "1999-06-07"
    assert D.pick_default(dates[:3]) == "1999-06-08"
    assert D.pick_default([{"date": "2003", "source": "tag"},
                           {"date": "2003-06-27", "source": "说明文件"}]) == "2003-06-27"
    assert D.pick_default([]) == ""


def test_search_album_strips_wiki_disambiguation(monkeypatch):
    monkeypatch.setattr(D, "artist_albums", lambda q: [
        {"qid": "Q1", "labels": ["勇敢 (張惠妹專輯)", "Brave"], "dates": []},
        {"qid": "Q2", "labels": ["姊妹 (專輯)", "Sisters"], "dates": []},
    ])
    assert D.search_album(["勇敢"], "Q713461") == "Q1"
    assert D.search_album(["姊妹"], "Q713461") == "Q2"
    assert D.search_album(["Sisters"], "Q713461") == "Q2"
    assert D.search_album(["勇敢"], "") == ""                    # 不知道歌手的 Wikidata 条目就不猜


def test_claim_dates_respect_precision():
    def claim(t, p):
        return {"mainsnak": {"datavalue": {"value": {"time": t, "precision": p}}}}
    ent = {"claims": {"P577": [claim("+1998-10-12T00:00:00Z", 11), claim("+1998-10-00T00:00:00Z", 10),
                               claim("+1997-00-00T00:00:00Z", 9)]}}
    assert D._claim_dates(ent) == ["1998-10-12", "1998-10", "1997"]
