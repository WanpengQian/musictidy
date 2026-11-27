"""限定范围外部确认 (整理第 3 层) —— 已知歌手, 只在他自己的专辑里找, 用时长序列验证.

不是"从零识别", 而是给第 1 层解析出的 (歌手, 专辑, 曲目时长) 找 MB 上对应的那张:
1. 目录里有 Picard 写的 musicbrainz_* tag → 直接拿那个 release group (method=mbtag)
2. 歌手 mbid (别名表 / 搜 MB) → browse 该歌手全部 release group → 专辑名归一化匹配
   (简繁/大小写/前缀, 所有专辑票都试: `牵手` 和 `Holding Hands` 都算)
   → 候选 rg 的各 release 曲目表 vs 目录时长序列打分 (method=title)
3. 标题对不上 → 用各轨时长拼近似 TOC, 走 MB 模糊 DiscID 查询, 且要求歌手对得上 (method=toc)

打分 = 目录每一轨 (碟,轨) 跟 release 对应位置时长差 ≤ max(3s, 2%) 的比例;
≥0.85 confirmed, ≥0.6 probable。找不到不影响入库 (本地解析结果照样可批)。

MB 响应缓存进 mb_cache 表 (30 天), 确认结果缓存进 album_confirm 表 (按目录签名),
所以同一批收件箱第二次跑不联网。MB 限速 1 req/s 由 musicbrainzngs 自带节流保证。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import text

from app.db import get_engine
from app.folder_parse import ParsedAlbum, norm_key

log = logging.getLogger(__name__)

CACHE_TTL = 30 * 24 * 3600
CONFIRMED, PROBABLE = 0.85, 0.6
_MAX_RG_PAGES = 4          # browse rg 最多 4 页 × 100
_MAX_CANDIDATE_RGS = 5


# ── 缓存 ────────────────────────────────────────────────────────
def _ensure_tables(conn) -> None:
    conn.execute(text(
        """CREATE TABLE IF NOT EXISTS mb_cache (
               key        TEXT PRIMARY KEY,
               json       TEXT NOT NULL,
               fetched_at INTEGER NOT NULL
           )"""
    ))
    conn.execute(text(
        """CREATE TABLE IF NOT EXISTS album_confirm (
               dir        TEXT PRIMARY KEY,
               sig        TEXT NOT NULL,
               json       TEXT NOT NULL,
               updated_at INTEGER NOT NULL
           )"""
    ))


def _cached(key: str, fetch: Callable[[], Any]) -> Any:
    with get_engine().begin() as conn:
        _ensure_tables(conn)
        row = conn.execute(
            text("SELECT json, fetched_at FROM mb_cache WHERE key=:k"), {"k": key},
        ).first()
    if row and row[1] > time.time() - CACHE_TTL:
        return json.loads(row[0])
    data = fetch()
    if data is not None:
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    """INSERT INTO mb_cache (key, json, fetched_at) VALUES (:k, :j, :t)
                       ON CONFLICT(key) DO UPDATE SET json=:j, fetched_at=:t"""
                ),
                {"k": key, "j": json.dumps(data, ensure_ascii=False), "t": int(time.time())},
            )
    return data


def _mb():
    import musicbrainzngs  # noqa: PLC0415

    from app.workers.musicbrainz import _configure_user_agent  # noqa: PLC0415

    _configure_user_agent()
    return musicbrainzngs


def artist_release_groups(artist_mbid: str) -> list[dict[str, Any]]:
    """该歌手全部 release group (title / type / first-release-date)。"""
    def fetch() -> list[dict[str, Any]]:
        mb = _mb()
        out: list[dict[str, Any]] = []
        for page in range(_MAX_RG_PAGES):
            try:
                r = mb.browse_release_groups(artist=artist_mbid, limit=100, offset=page * 100)
            except Exception as e:  # noqa: BLE001
                log.info("album_confirm: browse rg %s: %s", artist_mbid, e)
                return out if out else None  # type: ignore[return-value]
            lst = r.get("release-group-list") or []
            out += [{"id": g["id"], "title": g.get("title", ""), "type": g.get("type", ""),
                     "date": g.get("first-release-date", "")} for g in lst]
            if len(out) >= int(r.get("release-group-count", 0)) or not lst:
                break
        return out
    return _cached(f"artist-rgs:{artist_mbid}", fetch) or []


def _simplify_release(rel: dict[str, Any]) -> dict[str, Any]:
    media = []
    for m in rel.get("medium-list") or []:
        tracks = []
        for t in m.get("track-list") or []:
            rec = t.get("recording") or {}
            ms = t.get("length") or rec.get("length")
            tracks.append({
                "position": int(t.get("position") or t.get("number") or 0),
                "title": t.get("title") or rec.get("title", ""),
                "recording_mbid": rec.get("id", ""),
                "length_s": round(int(ms) / 1000, 1) if ms else 0.0,
            })
        media.append({"position": int(m.get("position") or 1), "tracks": tracks})
    credits = [c["artist"] for c in rel.get("artist-credit") or [] if isinstance(c, dict) and "artist" in c]
    rg = rel.get("release-group") or {}
    return {
        "id": rel["id"], "title": rel.get("title", ""), "date": rel.get("date", ""),
        "country": rel.get("country", ""), "media": media,
        "rg_id": rg.get("id", ""), "rg_title": rg.get("title", ""),
        "artists": [{"id": a.get("id", ""), "name": a.get("name", "")} for a in credits],
    }


def rg_releases(rg_mbid: str) -> list[dict[str, Any]]:
    """某 release group 下所有 release 的曲目表 (含时长)。"""
    def fetch() -> list[dict[str, Any]] | None:
        mb = _mb()
        try:
            r = mb.browse_releases(release_group=rg_mbid, limit=50,
                                   includes=["recordings", "artist-credits", "media"])
        except Exception as e:  # noqa: BLE001
            log.info("album_confirm: browse releases %s: %s", rg_mbid, e)
            return None
        rels = [_simplify_release(x) for x in r.get("release-list") or []]
        for x in rels:
            x["rg_id"] = x["rg_id"] or rg_mbid
        return rels
    return _cached(f"rg-releases:{rg_mbid}", fetch) or []


def toc_lookup(toc: str) -> list[dict[str, Any]]:
    """MB 模糊 TOC 查询 (discid='-' + toc) → 候选 release。"""
    def fetch() -> list[dict[str, Any]] | None:
        mb = _mb()
        try:
            r = mb.get_releases_by_discid("-", toc=toc, cdstubs=False,
                                          includes=["recordings", "artist-credits", "release-groups"])
        except Exception as e:  # noqa: BLE001
            log.info("album_confirm: toc %s: %s", toc, e)
            return None
        return [_simplify_release(x) for x in r.get("release-list") or []]
    return _cached(f"toc:{toc}", fetch) or []


# ── 打分 ────────────────────────────────────────────────────────
def _folder_slots(album: ParsedAlbum) -> dict[tuple[int, int], dict[str, Any]]:
    """目录的 (碟,轨) → {时长, 标题}; 同轨多格式取最长时长 (有损头部估算常偏短)。
    没轨号的按文件顺序补。"""
    slots: dict[tuple[int, int], dict[str, Any]] = {}
    seq: dict[int, int] = {}
    for t in album.tracks:
        no = t.track
        if not no:
            seq[t.disc] = seq.get(t.disc, 0) + 1
            no = seq[t.disc]
        k = (t.disc, no)
        cur = slots.get(k)
        if cur is None or t.duration > cur["duration"]:
            slots[k] = {"duration": t.duration, "title": t.title}
    return slots


def _dur_ok(a: float, b: float) -> bool:
    return a > 0 and b > 0 and abs(a - b) <= max(3.0, 0.02 * b)


def score_release(album: ParsedAlbum, rel: dict[str, Any]) -> dict[str, Any]:
    """目录 vs release: 时长逐轨对位 + 标题辅助。碟号按 release 的 medium position 对齐;
    目录只有部分碟 (3/4 碟) 时只比有的那几碟。"""
    slots = _folder_slots(album)
    media = {m["position"]: {t["position"]: t for t in m["tracks"]} for m in rel["media"]}
    folder_discs = sorted({d for d, _ in slots})
    # 目录单碟但 release 碟号不从 1 起 / 目录碟号是 1 而 release 只有一碟 → 直接按顺序对
    if len(folder_discs) == len(media) and folder_discs != sorted(media):
        mapping = dict(zip(folder_discs, sorted(media), strict=True))
    else:
        mapping = {d: d for d in folder_discs}
    matched = title_hits = 0
    rel_tracks_in_scope = 0
    for d in folder_discs:
        rel_d = media.get(mapping[d])
        if rel_d:
            rel_tracks_in_scope += len(rel_d)
    for (d, no), info in slots.items():
        rt = (media.get(mapping[d]) or {}).get(no)
        if not rt:
            continue
        if _dur_ok(info["duration"], rt["length_s"]):
            matched += 1
        if info["title"] and norm_key(info["title"]) == norm_key(rt["title"]):
            title_hits += 1
    denom = max(len(slots), rel_tracks_in_scope, 1)
    # 没时长 (读不到) 时退回标题
    has_dur = any(v["duration"] for v in slots.values())
    score = matched / denom if has_dur else title_hits / denom
    return {
        "score": round(score, 3), "matched": matched, "title_hits": title_hits,
        "folder_tracks": len(slots), "release_tracks": rel_tracks_in_scope,
        "folder_discs": len(folder_discs), "release_discs": len(media),
        "disc_map": {str(k): v for k, v in mapping.items()},
    }


def _title_score(cands: list[str], title: str) -> float:
    k = norm_key(title)
    if not k:
        return 0.0
    best = 0.0
    for c in cands:
        ck = norm_key(c)
        if not ck:
            continue
        if ck == k:
            return 1.0
        if min(len(ck), len(k)) >= 2 and (ck.startswith(k) or k.startswith(ck)):
            best = max(best, 0.85)
        else:
            best = max(best, SequenceMatcher(None, ck, k).ratio() * 0.8)
    return best


def approx_toc(album: ParsedAlbum, disc: int = 1) -> str | None:
    """用某碟各轨时长拼近似 TOC (`1 N leadout off1 … offN`, 单位 CD 帧 1/75s, 起点 150)。
    轨号必须 1..N 连续, 否则不是整张碟, 不查。"""
    slots = {no: v["duration"] for (d, no), v in _folder_slots(album).items() if d == disc}
    if not slots or sorted(slots) != list(range(1, len(slots) + 1)):
        return None
    if any(v <= 0 for v in slots.values()):
        return None
    offs, pos = [], 150
    for no in range(1, len(slots) + 1):
        offs.append(pos)
        pos += int(round(slots[no] * 75))
    return " ".join(str(x) for x in [1, len(slots), pos, *offs])


# ── 主入口 ──────────────────────────────────────────────────────
def _sig(album: ParsedAlbum) -> str:
    h = hashlib.sha1()
    for t in sorted(album.tracks, key=lambda x: x.path):
        h.update(f"{t.path}|{t.duration}|{t.disc}|{t.track}".encode())
    h.update(f"{album.artist}|{album.album}".encode())
    return h.hexdigest()


def confirm(album: ParsedAlbum, *, use_cache: bool = True, allow_search: bool = True) -> dict[str, Any]:
    """给一个解析结果找 MB 上对应的 release。返回
    {status: confirmed/probable/none, method, score, rg_mbid, release_mbid, artist_mbid,
     artist_name, title, date, tracks[{disc,position,title,recording_mbid,length_s}], detail}。"""
    from app import artist_alias  # noqa: PLC0415

    sig = _sig(album)
    if use_cache:
        with get_engine().begin() as conn:
            _ensure_tables(conn)
            row = conn.execute(
                text("SELECT sig, json FROM album_confirm WHERE dir=:d"),
                {"d": f"{album.dir}|{album.album}"},
            ).first()
        if row and row[0] == sig:
            return json.loads(row[1])

    idx = artist_alias.index()
    tried: list[str] = []
    best: tuple[dict[str, Any], dict[str, Any], str] | None = None   # (release, score, method)

    def consider(rel: dict[str, Any], method: str) -> None:
        nonlocal best
        sc = score_release(album, rel)
        if best is None or (sc["score"], sc["title_hits"]) > (best[1]["score"], best[1]["title_hits"]):
            best = (rel, sc, method)

    # 1. Picard tag 已带 rg
    rg_tag = album.mb.get("mb_rgid")
    if rg_tag:
        for rel in rg_releases(rg_tag):
            consider(rel, "mbtag")
        tried.append(f"mbtag rg {rg_tag[:8]}")

    # 2. 歌手范围内按专辑名找
    artist_mbid = ""
    hit = idx.resolve(album.artist)
    if hit and hit[1]:
        artist_mbid = hit[1]
    elif album.artist and allow_search:
        r = artist_alias.search_mb(album.artist)
        if r and r[1]:
            artist_mbid = r[1]
            idx = artist_alias.index()
    if (best is None or best[1]["score"] < CONFIRMED) and artist_mbid:
        titles = [album.album] + [v["value"] for v in album.album_votes]
        rgs = artist_release_groups(artist_mbid)
        ranked = sorted(((_title_score(titles, g["title"]), g) for g in rgs),
                        key=lambda x: -x[0])
        cands = [g for s, g in ranked if s >= 0.6][:_MAX_CANDIDATE_RGS]
        tried.append(f"歌手 {len(rgs)} 张专辑, 名字对上 {len(cands)} 张")
        for g in cands:
            for rel in rg_releases(g["id"]):
                rel.setdefault("rg_title", g["title"])
                consider(rel, "title")
            if best and best[1]["score"] >= CONFIRMED:
                break

    # 3. 近似 TOC (每碟一次, 要求歌手对得上)
    if best is None or best[1]["score"] < CONFIRMED:
        for d in sorted({t.disc for t in album.tracks}):
            toc = approx_toc(album, d)
            if not toc:
                continue
            rels = toc_lookup(toc)
            tried.append(f"TOC 碟{d}: {len(rels)} 个候选")
            for rel in rels:
                ok = any(
                    (artist_mbid and a["id"] == artist_mbid)
                    or idx.key(a["name"]) == idx.key(album.artist)
                    for a in rel["artists"]
                )
                if ok:
                    consider(rel, "toc")

    result: dict[str, Any] = {"status": "none", "method": "", "score": 0.0, "detail": tried,
                              "artist_mbid": artist_mbid}
    if best:
        rel, sc, method = best
        status = ("confirmed" if sc["score"] >= CONFIRMED
                  else "probable" if sc["score"] >= PROBABLE else "none")
        if status != "none":
            inv = {int(v): int(k) for k, v in sc["disc_map"].items()}
            tracks = [
                {"disc": inv.get(m["position"], m["position"]), "position": t["position"],
                 "title": t["title"], "recording_mbid": t["recording_mbid"],
                 "length_s": t["length_s"]}
                for m in rel["media"] for t in m["tracks"]
            ]
            a0 = rel["artists"][0] if rel["artists"] else {}
            result.update({
                "status": status, "method": method, "score": sc["score"], "match": sc,
                "rg_mbid": rel.get("rg_id", ""), "release_mbid": rel["id"],
                "artist_mbid": artist_mbid or a0.get("id", ""),
                "artist_name": a0.get("name", ""),
                "title": rel.get("rg_title") or rel["title"], "date": rel.get("date", ""),
                "tracks": tracks,
            })
            if status == "confirmed":
                _store_rg(result)
                backfill_library(album.artist, album.album, result)
    # 各来源的发行日期 (MB 首发 / 该版本 / Wikidata), 供用户挑年份
    try:
        from app import album_dates  # noqa: PLC0415

        result["dates"] = album_dates.online_dates(album, result)
    except Exception as e:  # noqa: BLE001
        log.info("album_confirm: dates %s: %s", album.dir, e)
        result["dates"] = []
    with get_engine().begin() as conn:
        _ensure_tables(conn)
        conn.execute(
            text(
                """INSERT INTO album_confirm (dir, sig, json, updated_at) VALUES (:d, :s, :j, :t)
                   ON CONFLICT(dir) DO UPDATE SET sig=:s, json=:j, updated_at=:t"""
            ),
            {"d": f"{album.dir}|{album.album}", "s": sig,
             "j": json.dumps(result, ensure_ascii=False), "t": int(time.time())},
        )
    return result


def ensure_artist_full(mbid: str) -> bool:
    """歌手缓存行不存在 / 只有名字 (没地区没排序名) → 从 MB 拉完整信息。
    播放器按地区分组 (CN/TW/HK → 中国) 靠 country, 只写名字的行会让歌手掉出分组。"""
    from sqlalchemy import text as _t  # noqa: PLC0415

    from app.workers.musicbrainz import _upsert_artist  # noqa: PLC0415

    if not mbid:
        return False
    with get_engine().connect() as conn:
        row = conn.execute(_t("SELECT country, sort_name FROM mb_artist WHERE mbid=:m"),
                           {"m": mbid}).first()
    if row and (row[0] or row[1]):
        return True
    try:
        a = _mb().get_artist_by_id(mbid, includes=["tags"])["artist"]
    except Exception as e:  # noqa: BLE001
        log.info("album_confirm: artist %s: %s", mbid, e)
        return False
    _upsert_artist(mbid, a)
    return True


def _store_rg(res: dict[str, Any]) -> None:
    """确认了的 rg 写进 mb_release_group 缓存 (含 tracks_json), approve 命名/曲序直接用。
    已有 tracks_json 的不覆盖 (别的流程可能按别的 release 填过)。"""
    rg, artist = res.get("rg_mbid"), res.get("artist_mbid")
    if not rg or not artist:
        return
    tracks = json.dumps([
        {"disc": t["disc"], "position": t["position"], "recording_mbid": t["recording_mbid"],
         "title": t["title"], "length_s": t["length_s"]}
        for t in res["tracks"]
    ], ensure_ascii=False)
    # 先走现有的 ensure_rg_in_cache: 从 MB 拉 rg 真实 primary/secondary type + 歌手行,
    # 播放器的专辑列表按 type (Album/EP) 过滤, 自己插一行空 type 会让专辑看不见。
    from app.workers.sync_sidecars import ensure_rg_in_cache  # noqa: PLC0415

    ok = ensure_rg_in_cache(rg)
    with get_engine().begin() as conn:
        if not ok:   # MB 拉不到: 兜底自己建 (type 按 Album, 至少能显示)
            conn.execute(text(
                """INSERT OR IGNORE INTO mb_artist
                       (mbid, name, sort_name, country, disambiguation, fetched_at, stale_after, genres)
                   VALUES (:m, :n, :n, '', '', strftime('%s','now'), 0, '[]')"""
            ), {"m": artist, "n": res.get("artist_name", "")})
            conn.execute(text(
                """INSERT OR IGNORE INTO mb_release_group
                       (mbid, artist_mbid, title, primary_type, secondary_types, first_release_date)
                   VALUES (:m, :a, :t, 'Album', '[]', :d)"""
            ), {"m": rg, "a": artist, "t": res.get("title", ""), "d": res.get("date", "")})
        conn.execute(text(
            """UPDATE mb_release_group SET
                   tracks_json=COALESCE(NULLIF(tracks_json, ''), :tj),
                   artist_name=CASE WHEN artist_name='' THEN :an ELSE artist_name END
               WHERE mbid=:m"""
        ), {"m": rg, "tj": tracks, "an": res.get("artist_name", "")})
    ensure_artist_full(artist)


def apply_confirmation(album: ParsedAlbum, res: dict[str, Any]) -> None:
    """把确认结果回写进解析结果: 专辑名/年份用 MB 的, 曲名补空缺, 置信度提到 high。
    歌手名不动 (保持 Library 规范名)。"""
    if res.get("status") != "confirmed":
        if res.get("status") == "probable":
            album.notes.append(f"MB 可能是《{res.get('title')}》(吻合 {res['score']:.0%})")
        return
    by_pos = {(t["disc"], t["position"]): t for t in res["tracks"]}
    for t in album.tracks:
        mt = by_pos.get((t.disc, t.track))
        if mt and (not t.title or t.title_source in ("fname", "log")):
            t.title, t.title_source = mt["title"], "mb"
    if res.get("title") and norm_key(res["title"]) != norm_key(album.album):
        # 本地多个来源一致 (收藏者 tag/CUE/目录都这么叫) → 保留本地名, MB 名只做备注;
        # 本地只有单一来源 / 有冲突 → 用 MB 的
        top = album.album_votes[0] if album.album_votes else None
        rival = album.album_votes[1] if len(album.album_votes) > 1 else None
        local_solid = bool(top and len(top["sources"]) >= 2
                           and not (rival and rival["weight"] >= 0.6 * top["weight"]))
        if local_solid:
            album.notes.append(f"MB 上叫《{res['title']}》, 保留本地名")
        else:
            album.notes.append(f"专辑名按 MB: {album.album}→{res['title']}")
            album.album = res["title"]
    # 年份: MB 首发 > 匹配到的版本 (可能是再版); 最终默认值由 album_dates.pick_default 统一定
    first = next((d["date"] for d in res.get("dates", []) if d["source"] == "MusicBrainz 首发"), "")
    if (first or res.get("date")) and not album.year:
        album.year = (first or res["date"])[:4]
    album.mb.setdefault("mb_rgid", res.get("rg_mbid", ""))
    album.mb.setdefault("mb_albumid", res.get("release_mbid", ""))
    m = res["match"]
    album.notes.append(
        f"MB 确认 ({res['method']}) 时长吻合 {m['matched']}/{m['folder_tracks']}"
        + (f", 目录 {m['folder_discs']} 碟 / 发行 {m['release_discs']} 碟"
           if m["folder_discs"] != m["release_discs"] else "")
    )
    album.notes = [n for n in album.notes if not n.startswith(("专辑有冲突", "歌手有冲突"))]
    album.confidence = "high"


def cached_result(album: ParsedAlbum) -> dict[str, Any] | None:
    """只读缓存 (不联网): 这个目录之前确认过且文件没变 → 结果, 否则 None。"""
    with get_engine().begin() as conn:
        _ensure_tables(conn)
        row = conn.execute(
            text("SELECT sig, json FROM album_confirm WHERE dir=:d"),
            {"d": f"{album.dir}|{album.album}"},
        ).first()
    if row and row[0] == _sig(album):
        return json.loads(row[1])
    return None


# ── 已入库专辑补 MB 身份 ──────────────────────────────────────────
def backfill_library(artist: str, album: str, res: dict[str, Any]) -> int:
    """这张专辑已经在 Library 里 (先批准、后联网确认) 但没 rg → 把 rg + 逐轨 recording 写回 beets。
    不然播放器只能走本地兜底视图, 没封面没身份。返回改了几首。"""
    from app import artist_alias, beets_bridge, library_match  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    rg = res.get("rg_mbid", "")
    if res.get("status") != "confirmed" or not rg:
        return 0
    s = get_settings()
    lib = beets_bridge.get_library(s.beets_db, s.music_root)
    idx = artist_alias.index()
    la = library_match.build_index(lib, idx.key).match(artist, album)
    if la is None or la.rg == rg:
        return 0
    rec_by_pos = {(t["disc"], t["position"]): t["recording_mbid"] for t in res.get("tracks", [])}
    n = 0
    for (disc, no), t in la.tracks.items():
        if beets_bridge.set_item_meta(
            lib, t.item_id,
            releasegroup_mbid=rg,
            track_mbid=rec_by_pos.get((disc, no)) or None,
            album_artist_mbid=res.get("artist_mbid") or None,
        ):
            n += 1
    if n:
        log.info("album_confirm: 补 rg %s → %s / %s (%d 首)", rg[:8], artist, album, n)
    return n


def backfill_all_from_cache() -> dict[str, int]:
    """一次性: 把缓存里所有 confirmed 的结果对 Library 跑一遍 backfill (老数据修复用)。"""
    out: dict[str, int] = {}
    with get_engine().connect() as conn:
        _ensure_tables(conn)
        rows = conn.execute(text("SELECT dir, json FROM album_confirm")).fetchall()
    for d, j in rows:
        res = json.loads(j)
        if res.get("status") != "confirmed":
            continue
        album = d.rsplit("|", 1)[-1]
        n = backfill_library(res.get("artist_name", ""), album, res)
        if n:
            out[f"{res.get('artist_name')} / {album}"] = n
    return out
