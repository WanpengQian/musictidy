"""专辑发行日期的联网来源 —— 每个来源各报一个日期, 让用户在 App 里挑。

- MusicBrainz 首发: release group 的 first-release-date (不是匹配到的那个再版的日期)
- MusicBrainz 该版本: 时长吻合的那个 release 的日期 (跟首发不同时才列, 多半是再版)
- Wikidata: 专辑条目的「发行日期」(P577)。条目优先用 MB release group 上挂的 Wikidata 链接;
  没链接再按专辑名 (简/繁都试) 搜, 且「演唱者」(P175) 必须是 MB 歌手链接到的那个 Wikidata
  条目 —— 名字普通的专辑 (《勇敢》《姊妹》) 不会串到别人头上。

iTunes 不用: 老专辑给的多是数字版上架日期。响应都走 album_confirm 的 mb_cache (30 天)。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any

from app.album_confirm import _cached, _mb
from app.folder_parse import ParsedAlbum, norm_key

log = logging.getLogger(__name__)

SRC_MB_FIRST = "MusicBrainz 首发"
SRC_MB_RELEASE = "MusicBrainz 该版本"
SRC_WIKIDATA = "Wikidata"

# 默认选哪个: 权威的首发日期优先; 再版日期垫底
PRIORITY = [SRC_MB_FIRST, SRC_WIKIDATA, "说明文件", "tag", "目录名", SRC_MB_RELEASE]

_UA = "MusicTidy/0.1 ( https://musictidy.com )"
# 专辑 / 录音室专辑 / 合辑 / 现场专辑 / EP / 精选辑 / 原声带
_ALBUM_TYPES = {"Q482994", "Q208569", "Q222910", "Q209939", "Q169930", "Q134556", "Q4176708"}


def pick_default(dates: list[dict[str, str]]) -> str:
    """按来源优先级挑默认日期; 同优先级取更精确的 (带月日)。"""
    best: tuple[int, int, str] | None = None
    for d in dates:
        if not d.get("date"):
            continue
        pr = PRIORITY.index(d["source"]) if d["source"] in PRIORITY else len(PRIORITY)
        key = (pr, -len(d["date"]), d["date"])
        if best is None or key < best:
            best = key
    return best[2] if best else ""


# ── MusicBrainz ────────────────────────────────────────────────
def _wikidata_from_rels(rels: list[dict[str, Any]]) -> str:
    for r in rels or []:
        if r.get("type") == "wikidata":
            return (r.get("target") or "").rstrip("/").rsplit("/", 1)[-1]
    return ""


def rg_info(rg: str) -> dict[str, str]:
    """release group 首发日期 + 挂的 Wikidata 条目。"""
    def fetch() -> dict[str, str] | None:
        try:
            g = _mb().get_release_group_by_id(rg, includes=["url-rels"])["release-group"]
        except Exception as e:  # noqa: BLE001
            log.info("album_dates: rg %s: %s", rg, e)
            return None
        return {"date": g.get("first-release-date", ""),
                "wikidata": _wikidata_from_rels(g.get("url-relation-list"))}
    return _cached(f"rg-info:{rg}", fetch) or {}


def artist_wikidata(artist_mbid: str) -> str:
    def fetch() -> dict[str, str] | None:
        try:
            a = _mb().get_artist_by_id(artist_mbid, includes=["url-rels"])["artist"]
        except Exception as e:  # noqa: BLE001
            log.info("album_dates: artist %s: %s", artist_mbid, e)
            return None
        return {"wikidata": _wikidata_from_rels(a.get("url-relation-list"))}
    return (_cached(f"artist-wd:{artist_mbid}", fetch) or {}).get("wikidata", "")


# ── Wikidata ───────────────────────────────────────────────────
def _wd_get(params: dict[str, str]) -> dict[str, Any] | None:
    url = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode({**params, "format": "json"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": _UA}),
                                    timeout=20) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        log.info("album_dates: wikidata %s: %s", params.get("action"), e)
        return None


def _wd_entities(ids: list[str]) -> dict[str, Any]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    key = "wd-ent:" + "|".join(sorted(ids))
    data = _cached(key, lambda: _wd_get({"action": "wbgetentities", "ids": "|".join(ids),
                                         "props": "claims|labels", "languages": "zh|en|ja"}))
    return (data or {}).get("entities", {})


def _claim_ids(ent: dict[str, Any], prop: str) -> list[str]:
    out = []
    for c in ent.get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and v.get("id"):
            out.append(v["id"])
    return out


def _claim_dates(ent: dict[str, Any]) -> list[str]:
    """P577 发行日期 → YYYY[-MM[-DD]] (按 Wikidata 的精度: 9 年 / 10 月 / 11 日)。"""
    out = []
    for c in ent.get("claims", {}).get("P577", []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if not isinstance(v, dict) or not v.get("time"):
            continue
        t, prec = v["time"].lstrip("+"), int(v.get("precision", 11))
        d = t[:10] if prec >= 11 else t[:7] if prec == 10 else t[:4]
        if d[:4].isdigit() and d not in out:
            out.append(d)
    return out


def wikidata_dates(qid: str) -> list[str]:
    return _claim_dates(_wd_entities([qid]).get(qid, {}))


def artist_albums(artist_qid: str) -> list[dict[str, Any]]:
    """该歌手 (演唱者 P175) 在 Wikidata 上的全部专辑: [{qid, labels[], dates[]}]。
    一次 SPARQL 拿全, 比按名字搜稳 (《勇敢》《姊妹》这种名字搜索会被同名条目挤掉)。"""
    q = f"""SELECT ?a ?label ?date WHERE {{
      ?a wdt:P175 wd:{artist_qid} .
      ?a wdt:P31/wdt:P279* wd:Q482994 .
      OPTIONAL {{ ?a rdfs:label ?label . FILTER(LANG(?label) IN ("zh","zh-hant","zh-hans","zh-tw","zh-hk","zh-cn","en","ja")) }}
      OPTIONAL {{ ?a p:P577/psv:P577 [ wikibase:timeValue ?date ; wikibase:timePrecision ?prec ] . }}
    }}"""

    def fetch() -> list[dict[str, Any]] | None:
        url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": q, "format": "json"})
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": _UA, "Accept": "application/sparql-results+json"}),
                    timeout=30) as r:
                rows = json.load(r)["results"]["bindings"]
        except Exception as e:  # noqa: BLE001
            log.info("album_dates: sparql %s: %s", artist_qid, e)
            return None
        by: dict[str, dict[str, Any]] = {}
        for row in rows:
            qid = row["a"]["value"].rsplit("/", 1)[-1]
            it = by.setdefault(qid, {"qid": qid, "labels": [], "dates": []})
            lab = row.get("label", {}).get("value")
            if lab and lab not in it["labels"]:
                it["labels"].append(lab)
        return list(by.values())

    albums = _cached(f"wd-artist-albums:{artist_qid}", fetch) or []
    # 日期按精度取 (SPARQL 的 timeValue 不带精度, 单独从条目拿)
    ents = {}
    for i in range(0, len(albums), 50):
        ents.update(_wd_entities([a["qid"] for a in albums[i:i + 50]]))
    for a in albums:
        a["dates"] = _claim_dates(ents.get(a["qid"], {}))
    return albums


# 维基的消歧义后缀: 「姊妹 (專輯)」「Bad Boy (張惠妹專輯)」「Star (album)」
_DISAMBIG = re.compile(r"\s*[(（][^()（）]*(?:專輯|专辑|album|アルバム|EP|單曲|单曲)[^()（）]*[)）]\s*$", re.I)


def search_album(titles: list[str], artist_qid: str) -> str:
    """在该歌手的 Wikidata 专辑里按名字找 (归一化后相等, 简繁/大小写/标点无关)。"""
    if not artist_qid:
        return ""
    want = {norm_key(t) for t in titles if t}
    for a in artist_albums(artist_qid):
        keys = set()
        for lab in a["labels"]:
            keys.add(norm_key(lab))
            keys.add(norm_key(_DISAMBIG.sub("", lab)))   # 「勇敢 (張惠妹專輯)」→「勇敢」
        if keys & want:
            return a["qid"]
    return ""


# ── 汇总 ───────────────────────────────────────────────────────
def online_dates(album: ParsedAlbum, res: dict[str, Any]) -> list[dict[str, str]]:
    """联网来源的日期候选 (联网确认时跑一次, 跟确认结果一起缓存)。"""
    out: list[dict[str, str]] = []
    rg = res.get("rg_mbid") or album.mb.get("mb_rgid", "") if res.get("status") == "confirmed" \
        else album.mb.get("mb_rgid", "")
    info = rg_info(rg) if rg else {}
    if info.get("date"):
        out.append({"date": info["date"], "source": SRC_MB_FIRST})
    rel_date = res.get("date", "") if res.get("status") == "confirmed" else ""
    if rel_date and rel_date != info.get("date"):
        out.append({"date": rel_date, "source": SRC_MB_RELEASE})

    qid = info.get("wikidata", "")
    if not qid:
        artist_mbid = res.get("artist_mbid") or album.mb.get("mb_albumartistid", "")
        aq = artist_wikidata(artist_mbid) if artist_mbid else ""
        titles = [album.album, res.get("title", "")] + [v["value"] for v in album.album_votes]
        qid = search_album([t for t in titles if t], aq)
    for d in wikidata_dates(qid) if qid else []:
        out.append({"date": d, "source": SRC_WIKIDATA, "detail": qid})
    return out
