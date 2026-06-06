"""Worker: 把当前 beets 识别结果同步成 .musictidy.json 写到各专辑目录。

scan 完会 enqueue 一个这样的任务。该 worker 先看队列里还有没有
fingerprint / cue_split 在跑, 有就 re-enqueue 自己 (晚 60s 再试),
等到队列空了, 才真正干活 — 此时识别结果稳定了, 同一个 dir 的所有
items 是不是绑到一致的 rg 一目了然, 不会写出半成品 sidecar。

写 sidecar 的规则:
- 该 dir 下 items 全部绑到同一个 rg → 写 sidecar
- 混着多个 rg 或者部分 unbound → 跳过 (这张专辑还在变化, 不能定结论)
"""

from __future__ import annotations

import logging
import os as _os
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app import info_sidecar
from app.db import get_engine

log = logging.getLogger(__name__)

# 一张正常专辑顶多 ~30 轨; ≥这个数基本是盒装 / 超大精选 / 厂牌大碟。
_MEGA_RG_TRACKS = 40
# 文件夹要"拥有"这种 mega 发行的大部分曲目, 才认你是它的主人; 否则八成是
# AcoustID 把你的曲指纹命中了"恰好也被收进这套大合辑"的录音, 按 filename
# 序号硬绑进去 = 标题全错 + (镜像碟)重复计数。
_MEGA_COVERAGE = 0.6


def is_incidental_mega_match(total_tracks: int, n_files: int) -> bool:
    """判定: n_files 个文件被指向一个 total_tracks 轨的发行, 这是不是
    "指纹偶然命中大合辑里的录音"而非真拥有。

    True = 发行是 mega 合辑/盒装 (≥_MEGA_RG_TRACKS 轨) 且本文件夹只占其中
    一小撮 (<_MEGA_COVERAGE) → 不该按位置硬绑 (典型: 44 个现场录音文件被吸
    进 222 轨的《君之頌讚四》盒装)。
    False = 普通专辑 (哪怕只拥有几轨的残缺专辑, 仍可正常按位置补全)。
    """
    if total_tracks < _MEGA_RG_TRACKS:
        return False
    return n_files < total_tracks * _MEGA_COVERAGE


def unbind_incidental_mega_matches() -> int:
    """库级 sweep: 任何 item 当前绑在 mega 发行 (≥40 轨) 上、但其所在文件夹只
    占该发行 <60% → 解绑。

    dominant-per-folder 投票护栏只管"投票投出来的 dominant"; 但有些 item 的
    mega rg 是 beets 直接打的 MB tag, candidate_rgs 里根本没它, 投票看不见 →
    护栏够不着 (典型: 陈慧娴[分轨] 17 文件被 beets 直接标成 123 轨《大盛期》)。
    这个 sweep 直接按"当前绑定 + 文件夹覆盖率"判, 把这类也清掉。

    解绑后 item 退回 owned-albums 的本地兜底文件夹专辑 (仍可见可播)。源文件
    不动。返回解绑的 item 数。
    """
    import json as _json  # noqa: PLC0415
    import os as _os  # noqa: PLC0415
    from collections import defaultdict as _dd  # noqa: PLC0415

    from app import beets_bridge as _bb  # noqa: PLC0415
    from app.config import get_settings as _gs  # noqa: PLC0415

    s = _gs()
    lib = _bb.get_library(s.beets_db, s.music_root)
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            """SELECT i.id, i.path, i.mb_releasegroupid AS rg,
                      rg.tracks_json AS tj
               FROM beets.items i
               JOIN mb_release_group rg ON rg.mbid = i.mb_releasegroupid
               WHERE i.mb_releasegroupid != '' AND rg.tracks_json IS NOT NULL"""
        )).all()

    groups: dict[tuple[str, str], list[int]] = _dd(list)
    rg_total: dict[str, int] = {}
    for r in rows:
        raw = r.path
        p = bytes(raw).decode("utf-8", "replace") \
            if isinstance(raw, (bytes, memoryview)) else (raw or "")
        groups[(_os.path.dirname(p), r.rg)].append(int(r.id))
        if r.rg not in rg_total:
            try:
                v = _json.loads(r.tj)
                rg_total[r.rg] = len(v) if isinstance(v, list) else 0
            except (TypeError, ValueError):
                rg_total[r.rg] = 0

    freed = 0
    for (folder, rg), ids in groups.items():
        if not is_incidental_mega_match(rg_total.get(rg, 0), len(ids)):
            continue
        n = 0
        for iid in ids:
            # 只清 rg + track; 保留 artist (见上方护栏同款理由), 让它退回该
            # 艺人名下的文件夹兜底专辑, 而不是掉进「未识别」。
            if _bb.set_mb_ids(
                lib, iid, track_mbid="", releasegroup_mbid="",
                artist_mbid=None, album_artist_mbid=None, album_artist=None,
            ):
                n += 1
        freed += n
        log.info(
            "sync_sidecars[sweep]: 解绑 %s (mega %s %d轨, 本夹 %d文件)",
            _os.path.basename(folder)[:40], rg[:8], rg_total.get(rg, 0), n,
        )
    if freed:
        log.info("sync_sidecars[sweep]: mega 误绑共解绑 %d 个 item", freed)
    return freed


def ensure_rg_in_cache(rg_mbid: str) -> bool:
    """确保 mb_release_group 表里有 rg_mbid 这一行 (含 artist FK)。

    owned-albums endpoint 起手就 FROM mb_release_group, 缺这行整张专辑就不
    显示。item 的 mb_releasegroupid 可能是 beets 导入时直接带的 MB tag, 而
    server 侧这张缓存表从没为它填过行 —— 那张专辑就凭空消失。这个函数按需
    从 MB 拉一次补上。

    返回 True = 调用后表里确实有这行 (本来就有 / 这次补上了);
    返回 False = 补不上 (MB 没 artist-credit / 网络失败), 调用方自行兜底。
    """
    import json as _json  # noqa: PLC0415
    import urllib.request as _ur  # noqa: PLC0415

    with get_engine().connect() as conn:
        if conn.execute(
            text("SELECT 1 FROM mb_release_group WHERE mbid=:m"),
            {"m": rg_mbid},
        ).first():
            return True
    try:
        url = (f"https://musicbrainz.org/ws/2/release-group/{rg_mbid}"
               f"?inc=artist-credits&fmt=json")
        with _ur.urlopen(
            _ur.Request(url, headers={"User-Agent": "MusicTidy/0.1"}),
            timeout=15,
        ) as resp:
            d = _json.load(resp)
        ac = (d.get("artist-credit") or [{}])[0].get("artist") or {}
        artist_mbid = ac.get("id") or ""
        if not artist_mbid:
            return False
        with get_engine().begin() as conn:
            # mb_artist FK 必须存在
            if not conn.execute(
                text("SELECT 1 FROM mb_artist WHERE mbid=:m"), {"m": artist_mbid},
            ).first():
                conn.execute(text("""
                  INSERT OR IGNORE INTO mb_artist
                    (mbid, name, sort_name, country, disambiguation,
                     fetched_at, stale_after, genres)
                  VALUES (:m, :n, :s, :c, :d,
                          strftime('%s','now'),
                          strftime('%s','now')+604800, '[]')
                """), {
                    "m": artist_mbid,
                    "n": ac.get("name", ""),
                    "s": ac.get("sort-name", ""),
                    "c": ac.get("country", "") or "",
                    "d": ac.get("disambiguation", "") or "",
                })
            conn.execute(text("""
              INSERT OR IGNORE INTO mb_release_group
                (mbid, artist_mbid, title, primary_type, secondary_types,
                 first_release_date)
              VALUES (:m, :a, :t, :p, :s, :d)
            """), {
                "m": rg_mbid, "a": artist_mbid,
                "t": d.get("title", ""),
                "p": d.get("primary-type", "") or "",
                "s": _json.dumps(d.get("secondary-types") or []),
                "d": d.get("first-release-date", "") or "",
            })
        return True
    except Exception:  # noqa: BLE001
        return False


def backfill_missing_release_groups(limit: int | None = None) -> dict[str, int]:
    """扫 beets.items 里所有非空 mb_releasegroupid, 把缓存表里缺的逐个补上。

    专治「item 有 releasegroupid 但 mb_release_group 没这行 → 整张专辑在
    Browse 里隐身」(典型如张学友: 12 个 item 全在, 但一张专辑都不显示)。

    MB ws/2 限速 1 req/s, 这里每补一个 sleep 1.1s, 所以是慢活、跑后台。
    返回 {scanned, filled, failed}。
    """
    import time as _time  # noqa: PLC0415

    with get_engine().connect() as conn:
        missing = [r[0] for r in conn.execute(text(
            """SELECT DISTINCT mb_releasegroupid
               FROM beets.items
               WHERE mb_releasegroupid IS NOT NULL
                 AND mb_releasegroupid != ''
                 AND mb_releasegroupid NOT IN
                     (SELECT mbid FROM mb_release_group)"""
        )).all()]
    if limit is not None:
        missing = missing[:limit]
    filled = failed = 0
    for i, rg in enumerate(missing):
        if ensure_rg_in_cache(rg):
            filled += 1
        else:
            failed += 1
            log.warning("backfill: 补不上 rg=%s", rg)
        if i < len(missing) - 1:
            _time.sleep(1.1)  # 尊重 MB 1 req/s
    log.info(
        "backfill_missing_release_groups: scanned=%d filled=%d failed=%d",
        len(missing), filled, failed,
    )
    return {"scanned": len(missing), "filled": filled, "failed": failed}


def _consolidate_by_folder() -> None:
    """跨同 dir items 的 AcoustID candidate_rgs 求交集 + 投票, 把被错识到
    best-of 合集的 item 拉回真正的原专辑 rg.

    规则:
    1. 同 dir 收集所有 items 的 candidate_rgs (AcoustID 给的全部候选)
    2. 投票: 每个 rg 出现次数. 优先 rg 出现 ≥ 阈值 (50% items)
    3. 在高分 rg 里挑非 Compilation 的 → 那就是真原专辑
    4. dir 下 item 当前 rg 是 Compilation 而真正候选可达 → 改绑到原专辑
    5. 未绑 (rg='') 的 item 也直接绑到原专辑

    不动: 当前已绑到非 Compilation rg 的 item (信任 AcoustID 的 album 拾取)
    """
    import json as _json  # noqa: PLC0415
    import os as _os  # noqa: PLC0415
    from collections import Counter as _Counter, defaultdict as _dd  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    from sqlalchemy import text as _text  # noqa: PLC0415

    from app import beets_bridge as _bb  # noqa: PLC0415
    from app.config import get_settings as _gs  # noqa: PLC0415

    s = _gs()
    lib = _bb.get_library(s.beets_db, s.music_root)
    music_root = s.music_root

    # 拿 items + 各 item 的 candidate_rgs (track_fingerprint join 不上 beets,
    # 因为是两个 DB; 分两次查再 python 合)
    with get_engine().connect() as conn:
        item_rows = conn.execute(_text(
            """SELECT id, mb_releasegroupid, path FROM beets.items"""
        )).all()
        cand_rows = conn.execute(_text(
            """SELECT item_id, candidate_rgs FROM track_fingerprint
               WHERE candidate_rgs IS NOT NULL"""
        )).all()
        # rg 类型 (区分 Compilation vs 真专辑)
        rg_types_rows = conn.execute(_text(
            """SELECT mbid, primary_type, secondary_types
               FROM mb_release_group"""
        )).all()

    item_cand: dict[int, list[str]] = {}
    for r in cand_rows:
        try:
            v = _json.loads(r.candidate_rgs)
            if isinstance(v, list):
                item_cand[int(r.item_id)] = [m for m in v if m]
        except (TypeError, ValueError):
            continue

    # rg type cache: mbid → "compilation" / "album" / None (没拉到)
    _rg_type: dict[str, str | None] = {}
    for r in rg_types_rows:
        sec = r.secondary_types or ""
        _rg_type[r.mbid] = "compilation" if "Compilation" in sec else "album"

    def _is_compilation(rg_mbid: str) -> bool:
        if not rg_mbid:
            return False
        cached = _rg_type.get(rg_mbid)
        if cached is not None:
            return cached == "compilation"
        # 缓存 miss → 拉一次 MB 只在内存记 (mb_release_group 有 FK 约束到
        # mb_artist, 不想为了存 type 额外拉 artist 那一套, 太重)
        try:
            import urllib.request as _ur  # noqa: PLC0415
            url = f"https://musicbrainz.org/ws/2/release-group/{rg_mbid}?fmt=json"
            req = _ur.Request(url, headers={"User-Agent": "MusicTidy/0.1"})
            with _ur.urlopen(req, timeout=10) as resp:
                d = _json.load(resp)
            sec = d.get("secondary-types") or []
            kind = "compilation" if "Compilation" in sec else "album"
            _rg_type[rg_mbid] = kind
            return kind == "compilation"
        except Exception:  # noqa: BLE001
            _rg_type[rg_mbid] = None
            return False  # MB 拉不到, 保守当不是合集

    by_dir: dict[str, list[dict]] = _dd(list)
    for r in item_rows:
        raw = r.path
        if isinstance(raw, (bytes, memoryview)):
            p_str = bytes(raw).decode("utf-8", errors="replace")
        else:
            p_str = str(raw or "")
        if not p_str:
            continue
        pp = _Path(p_str)
        if not pp.is_absolute():
            pp = s.to_abs(pp)
        d = _os.path.dirname(str(pp))
        by_dir[d].append({
            "id": int(r.id),
            "rg": r.mb_releasegroupid or "",
            "candidates": item_cand.get(int(r.id), []),
        })

    # 缓存 rg → tracks_json 以便按 filename position 补 recording_mbid
    _rg_tracks: dict[str, list[dict]] = {}

    def _ensure_rg_in_cache(rg_mbid: str) -> None:
        # 委托给模块级实现 (backfill endpoint 也复用同一份, 避免两处逻辑漂移)
        ensure_rg_in_cache(rg_mbid)

    def _get_tracks(rg_mbid: str) -> list[dict]:
        if rg_mbid in _rg_tracks:
            return _rg_tracks[rg_mbid]
        # 先看本地 mb_release_group.tracks_json
        try:
            with get_engine().connect() as conn:
                row = conn.execute(_text(
                    "SELECT tracks_json FROM mb_release_group WHERE mbid=:m"
                ), {"m": rg_mbid}).first()
            if row and row.tracks_json:
                v = _json.loads(row.tracks_json)
                if isinstance(v, list):
                    tracks = sorted(v, key=lambda t: int(t.get("position") or 0))
                    _rg_tracks[rg_mbid] = tracks
                    return tracks
        except Exception:  # noqa: BLE001
            pass
        # MB 拉一次 (同步) — 拿 release+recordings, 头一条 release 当 canonical
        try:
            import urllib.request as _ur  # noqa: PLC0415
            url1 = (f"https://musicbrainz.org/ws/2/release-group/{rg_mbid}"
                    f"?inc=releases&fmt=json")
            with _ur.urlopen(
                _ur.Request(url1, headers={"User-Agent": "MusicTidy/0.1"}),
                timeout=15,
            ) as resp:
                d1 = _json.load(resp)
            releases = d1.get("releases") or []
            if not releases:
                _rg_tracks[rg_mbid] = []
                return []
            rel_id = releases[0]["id"]
            url2 = (f"https://musicbrainz.org/ws/2/release/{rel_id}"
                    f"?inc=recordings&fmt=json")
            with _ur.urlopen(
                _ur.Request(url2, headers={"User-Agent": "MusicTidy/0.1"}),
                timeout=15,
            ) as resp:
                d2 = _json.load(resp)
            tracks_out: list[dict] = []
            for medium in d2.get("media", []) or []:
                disc = int(medium.get("position", 1) or 1)
                for tr in medium.get("tracks", []) or []:
                    rec = tr.get("recording") or {}
                    length_ms = int(rec.get("length") or tr.get("length") or 0)
                    tracks_out.append({
                        "disc": disc,
                        "position": int(tr.get("position", 0) or 0),
                        "recording_mbid": rec.get("id") or "",
                        "title": rec.get("title") or tr.get("title", ""),
                        "length_s": length_ms / 1000.0 if length_ms else 0.0,
                    })
            tracks_out.sort(key=lambda t: int(t.get("position") or 0))
            _rg_tracks[rg_mbid] = tracks_out

            # 顺手把 tracks_json 写回 mb_release_group, 下次免 fetch
            try:
                with get_engine().begin() as conn:
                    conn.execute(_text(
                        "UPDATE mb_release_group SET tracks_json=:j WHERE mbid=:m"
                    ), {"j": _json.dumps(tracks_out, ensure_ascii=False), "m": rg_mbid})
            except Exception:  # noqa: BLE001
                pass
            return tracks_out
        except Exception:  # noqa: BLE001
            _rg_tracks[rg_mbid] = []
            return []

    # filename → position 提取 (跟 fingerprint worker 同一套正则)
    import re as _re  # noqa: PLC0415
    _POS_PATTERNS = (
        _re.compile(r"^\s*(\d{1,2})[.\s_-]"),
        _re.compile(r"Track\s*N[o.]?\s*(\d{1,2})", _re.I),
        _re.compile(r"Track[\s_-]+(\d{1,2})", _re.I),
    )
    _DISC_RE = _re.compile(r"(?:CD|Disc[\s_-]*)(\d{1,2})", _re.I)

    def _filename_position(name: str) -> int | None:
        for pat in _POS_PATTERNS:
            m = pat.search(name)
            if m:
                return int(m.group(1))
        return None

    def _disc_from_path(p: str) -> int:
        """路径里抓 disc 编号 (CD1/CD2/Disc 1/...); 抓不到默认 1."""
        for seg in reversed(_Path(p).parts):  # 优先靠近文件名的 dir
            m = _DISC_RE.search(seg)
            if m:
                return int(m.group(1))
        return 1

    def _tracks_lookup(tracks: list[dict], disc: int, pos: int) -> str:
        """按 (disc, pos) 在 tracks_json 里找 recording_mbid."""
        for t in tracks:
            td = int(t.get("disc", 1) or 1)
            tp = int(t.get("position") or 0)
            if td == disc and tp == pos:
                return t.get("recording_mbid") or ""
        return ""

    # 先一次性拉所有 item 的 track-level artist (V.A. dir 检测用).
    # 用 track_fingerprint.artist (AcoustID 缓存的原始 artist name) 而不是
    # beets.items.mb_artistid — 后者可能被前一次 V.A. 规则清空, 反复 sync
    # 时检测会失效。AcoustID 的 artist name 文本是持久 ground truth。
    item_artists: dict[int, str] = {}
    all_ids = [it["id"] for d_items in by_dir.values() for it in d_items]
    if all_ids:
        with get_engine().connect() as conn:
            from sqlalchemy import bindparam as _bp  # noqa: PLC0415
            stmt = _text(
                "SELECT item_id, artist FROM track_fingerprint WHERE item_id IN :ids"
            ).bindparams(_bp("ids", expanding=True))
            for row in conn.execute(stmt, {"ids": all_ids}):
                item_artists[int(row.item_id)] = (row.artist or "").strip().lower()

    total_changed = 0
    for d, items in by_dir.items():
        if len(items) < 3:
            continue

        # V.A. tribute / 各家天后金曲合集 检测: dir 内 ≥3 个不同 track-level
        # artist → 这 dir 是多艺人合集 (AcoustID 给每曲的"原作者"都不同).
        # 任何自动绑定都是错的: Compilation rg 八成是 best-of 误识, Album rg
        # 八成是某翻唱者的原专辑 (跟用户拥有的 dir 不是一张). 全 unbind 让
        # 用户进「未识别」组手动归属, 比留个噪音 1-item Album 干净得多。
        if len(items) >= 5:
            distinct_artists = {
                item_artists.get(it["id"], "")
                for it in items
            } - {""}
            if len(distinct_artists) >= 3:
                short = d.replace(str(music_root), "").lstrip("/")
                unbound = 0
                for it in items:
                    if it["rg"]:
                        # 清完整: rg + rec + 两个 artist field, 不留残留
                        # (artist list 是 COALESCE(albumartist, artist) 算的,
                        # 残留任何一个都会让该艺人继续在 Browse 里冒出来)
                        ok = _bb.set_mb_ids(
                            lib, it["id"],
                            track_mbid="",
                            releasegroup_mbid="",
                            artist_mbid="",
                            album_artist_mbid="",
                            album_artist="",
                        )
                        if ok:
                            unbound += 1
                log.info(
                    "sync_sidecars: %s V.A. dir (%d distinct artists), unbind %d items",
                    short[:60], len(distinct_artists), unbound,
                )
                total_changed += unbound
                continue

        # 投票: 每个 rg 在多少 item 的 candidates 里出现
        votes = _Counter()
        for it in items:
            for rg in set(it["candidates"]):
                votes[rg] += 1
        if not votes:
            continue
        # 找最优 dominant.
        # 优先非 Compilation 且 >= 50% items 投票. 找不到再看是不是
        # "这 dir 本身就是合集" 的情形 (best-of/compilation 集), 接受条件:
        #   Compilation top1 票数 >= 3 AND 至少 2x runner-up AND 没有任何
        #   Album rg 突破 30% (没 Album rg 在跟它抢). 不然就保持 skip,
        #   等用户手写 sidecar / 手动指认。
        threshold = max(2, len(items) // 2)
        ranked = sorted(votes.items(), key=lambda x: -x[1])
        dominant = None
        for rg_mbid, cnt in ranked:
            if cnt < threshold:
                break
            if not _is_compilation(rg_mbid):
                dominant = rg_mbid
                break
        if not dominant and ranked:
            top1_rg, top1_n = ranked[0]
            if _is_compilation(top1_rg) and top1_n >= 3:
                # 没有 Album rg 上 30% → 这 dir 看起来确实就是合集本身.
                # 接受 top1 不管"领先 runner-up 多少", 因为 runner-up 也可能
                # 是别的合集 (双 CD best-of 在多个 compilation rg 上都有曲)
                album_threshold = max(2, len(items) * 3 // 10)
                has_album_challenger = any(
                    n >= album_threshold and not _is_compilation(rg)
                    for rg, n in ranked
                )
                if not has_album_challenger:
                    dominant = top1_rg
        if not dominant:
            continue

        # 拉到 dominant 时同时按 filename position 顺手补 rec_mbid
        # → 真正 0 干预: 拷贝一次, scan 完就齐
        _ensure_rg_in_cache(dominant)  # owned-albums endpoint 起手就 FROM 这表
        dom_tracks = _get_tracks(dominant)

        # 护栏: dominant 是 mega 合辑/盒装, 但本文件夹只占其中一小撮 → 这是
        # AcoustID 把你的曲指纹命中了"恰好也收进这套大合辑"的录音, 并非你真
        # 拥有这套盒装。按 filename 序号硬绑进 222 轨盒装 = 标题全错 + 重复
        # 计数。拒绝绑定; 已经被错绑到它上面的 item 顺手解绑, 退回文件夹兜底
        # 专辑 (owned-albums 的本地兜底让它仍可见可播)。源文件不动。
        if dom_tracks and is_incidental_mega_match(len(dom_tracks), len(items)):
            freed = 0
            for it in items:
                if it["rg"] == dominant:
                    # 只清 rg + track 绑定; 保留 artist —— 这是单艺人文件夹, 艺人
                    # 本来就对 (mega rg 的主艺人 = 该艺人), 清掉会让整夹掉进
                    # 「未识别」, 而不是退回该艺人名下的文件夹兜底专辑。
                    ok = _bb.set_mb_ids(
                        lib, it["id"],
                        track_mbid="", releasegroup_mbid="",
                        artist_mbid=None, album_artist_mbid=None, album_artist=None,
                    )
                    if ok:
                        freed += 1
            short = d.replace(str(music_root), "").lstrip("/")
            log.info(
                "sync_sidecars: %s 拒绝吸进 mega RG %s (%d 轨, 本夹仅 %d 文件), "
                "解绑 %d", short[:50], dominant[:8], len(dom_tracks), len(items),
                freed,
            )
            total_changed += freed
            continue

        # dominant rg 的主艺人 → mb_albumartistid 必须等于这个值。
        # AcoustID 对合唱曲会把合作艺人 (而不是专辑主艺人) 塞到 album_artist_mbid,
        # 害得「孙燕姿」(等合唱者) 的 Browse 页里冒出张惠妹的专辑。修法:
        # rg 的 artist_mbid 才是规范的 album_artist。
        with get_engine().connect() as conn:
            row = conn.execute(_text(
                "SELECT artist_mbid FROM mb_release_group WHERE mbid=:m"
            ), {"m": dominant}).first()
        dom_artist_mbid = (row.artist_mbid if row else "") or ""

        # 给同 dir 的 item 准备 path → (filename, disc) 映射
        item_path: dict[int, tuple[str, int]] = {}
        for r in item_rows:
            iid = int(r.id)
            if iid not in {it["id"] for it in items}:
                continue
            raw = r.path
            if isinstance(raw, (bytes, memoryview)):
                p_str = bytes(raw).decode("utf-8", errors="replace")
            else:
                p_str = str(raw or "")
            pp = _Path(p_str)
            if not pp.is_absolute():
                pp = s.to_abs(pp)
                p_str = str(pp)
            item_path[iid] = (_os.path.basename(p_str), _disc_from_path(p_str))

        # 拿当前 dir 每个 item 现绑的 rec_mbid + albumartist_mbid
        item_curr_rec: dict[int, str] = {}
        item_curr_aa: dict[int, str] = {}
        with get_engine().connect() as conn:
            ids = [it["id"] for it in items]
            from sqlalchemy import bindparam as _bp  # noqa: PLC0415
            stmt = _text(
                "SELECT id, mb_trackid, mb_albumartistid FROM beets.items WHERE id IN :ids"
            ).bindparams(_bp("ids", expanding=True))
            for row in conn.execute(stmt, {"ids": ids}):
                item_curr_rec[int(row.id)] = row.mb_trackid or ""
                item_curr_aa[int(row.id)] = row.mb_albumartistid or ""

        # dominant 自己是 Compilation → 整 dir 就是合集本身, 应该把所有
        # item 都拉到 dominant 上 (包括当前指向另一张 Album rg 的那些).
        # dominant 是 Album → 默认保护已经在另一张 Album 上的 item (双 CD 混),
        # 除非 dominant 占比已经 ≥80% — 那种情况 outlier 几乎一定是
        # AcoustID 单 item 误绑, 不可能是真正第二张专辑共存。
        dominant_is_comp = _is_compilation(dominant)
        strong_dominant = votes[dominant] / len(items) >= 0.8

        changed = 0
        for it in items:
            cur = it["rg"]
            cur_rec = item_curr_rec.get(it["id"], "")
            cur_aa = item_curr_aa.get(it["id"], "")

            # albumartist 偏离 rg 主艺人 → 必须修正 (即便其他字段都已正确)
            aa_wrong = bool(dom_artist_mbid) and cur_aa != dom_artist_mbid
            aa_to_set = dom_artist_mbid if aa_wrong else None

            # 当前在 dominant + rec 已齐 + aa 也对 → 不动
            if cur == dominant and cur_rec and not aa_wrong:
                continue
            # dominant 是 Album, 当前 item 在另一张 Album → 不动 (合理共存);
            # 但 dominant 已 ≥80% 时认定 outlier 是噪音, 强拉过去。
            if (cur != dominant and cur
                and not _is_compilation(cur)
                and not dominant_is_comp
                and not strong_dominant):
                continue

            # 按 (disc, position) 试补 rec_mbid (双 CD 专辑要区分 disc)
            rec_for_item: str = cur_rec
            path_info = item_path.get(it["id"])
            if path_info and dom_tracks:
                fname, disc = path_info
                pos = _filename_position(fname)
                if pos is not None:
                    pos_rec = _tracks_lookup(dom_tracks, disc, pos)
                    if pos_rec:
                        rec_for_item = pos_rec

            # 已经在 dominant 且 rec 没变 且 aa 也对 → 不调用 (省一次 IO)
            if cur == dominant and rec_for_item == cur_rec and not aa_wrong:
                continue

            ok = _bb.set_mb_ids(
                lib, it["id"],
                track_mbid=rec_for_item,
                releasegroup_mbid=dominant,
                artist_mbid=None,
                album_artist_mbid=aa_to_set,
                album_artist=None,
            )
            if ok:
                changed += 1
        if changed:
            short = d.replace(str(music_root), "").lstrip("/")
            log.info(
                "sync_sidecars: 合并 %s → rg=%s (改了 %d 个 item)",
                short[:60], dominant[:8], changed,
            )
            total_changed += changed
    if total_changed:
        log.info("sync_sidecars: dominant-per-folder 共改了 %d 个 item", total_changed)

    # 库级 sweep: 投票护栏够不着的 mega 误绑 (beets 直接打的 MB tag,
    # candidate_rgs 里没这个 rg) 在这里按"当前绑定 + 覆盖率"兜底解绑。
    try:
        total_changed += unbind_incidental_mega_matches()
    except Exception:  # noqa: BLE001
        log.exception("sync_sidecars: mega sweep 失败 (不影响其余)")

    # 全局 normalize: 任何 item 只要绑了 rg, 它的 mb_albumartistid 必须等于
    # 该 rg 在 mb_release_group 里的主艺人 mbid. 这条独立于 dominant-per-folder
    # — best-of compilation dir 每曲散在不同 album rg 没 dominant, 合唱曲
    # AcoustID 写的 album_artist_mbid 又是合作艺人, 仍然会让"许茹芸/熊天平
    # 合唱"的 你的眼睛 在 Browse 里冒出"熊天平"艺人 tile。这条兜底拉正。
    norm_count = 0
    with get_engine().connect() as conn:
        rows = list(conn.execute(_text(
            """SELECT i.id, i.mb_releasegroupid, i.mb_albumartistid, rg.artist_mbid
               FROM beets.items i
               JOIN mb_release_group rg ON rg.mbid = i.mb_releasegroupid
               WHERE i.mb_releasegroupid != ''
                 AND rg.artist_mbid != ''
                 AND COALESCE(i.mb_albumartistid, '') != rg.artist_mbid"""
        )).all())
    for r in rows:
        ok = _bb.set_mb_ids(
            lib, int(r.id),
            track_mbid=None,
            releasegroup_mbid=None,
            artist_mbid=None,
            album_artist_mbid=r.artist_mbid,
            album_artist=None,
        )
        if ok:
            norm_count += 1
    if norm_count:
        log.info(
            "sync_sidecars: 全局 normalize mb_albumartistid → rg 主艺人, 改了 %d 个 item",
            norm_count,
        )

# 队列里还有这些类型的活就再等等 (sidecar 同步要稳态)
_BLOCKING_KINDS = ("fingerprint", "cue_split")
_POLL_INTERVAL_S = 30.0
_MAX_WAIT_S = 30 * 60  # 最长等 30 分钟, 否则放弃 (避免长尾任务卡死任务永不结束)


async def handle_sync_sidecars(payload: dict[str, Any]) -> None:  # noqa: ARG001
    import asyncio as _asyncio  # noqa: PLC0415

    from sqlalchemy import bindparam  # noqa: PLC0415

    # 在 worker 进程内 poll-with-sleep 等队列稳态, 别 re-enqueue 自己
    # (re-enqueue 没 delay 会瞬间打爆 task_queue 表)
    waited = 0.0
    while waited < _MAX_WAIT_S:
        with get_engine().connect() as conn:
            stmt = text(
                """SELECT COUNT(*) FROM task_queue
                   WHERE kind IN :kinds AND status IN ('queued','running')"""
            ).bindparams(bindparam("kinds", expanding=True))
            pending = conn.execute(stmt, {"kinds": list(_BLOCKING_KINDS)}).scalar() or 0
        if pending == 0:
            break
        log.info(
            "sync_sidecars: 队列还有 %d 个 fingerprint/cue_split, 等 %ds",
            pending, _POLL_INTERVAL_S,
        )
        await _asyncio.sleep(_POLL_INTERVAL_S)
        waited += _POLL_INTERVAL_S
    else:
        log.warning("sync_sidecars: 等了 %ds 队列仍未空, 强写一次就走人", _MAX_WAIT_S)

    # —— 第 0 步: dominant-per-folder 合并 —— 同 dir items 的 AcoustID
    # candidate_rgs 求交集投票, 自动把被识到 best-of 合集的 item 拉回
    # 真正的原专辑 rg. 跑这步之后, 之前 mixed_rg 的 dir 大多会变 pure,
    # 后面就能写出更多 sidecar。
    try:
        _consolidate_by_folder()
    except Exception:  # noqa: BLE001
        log.exception("sync_sidecars: dominant-per-folder 合并失败 (继续往下写 sidecar)")

    def _decode(p) -> str:
        if isinstance(p, (bytes, memoryview)):
            return bytes(p).decode("utf-8", errors="replace")
        return p or ""

    # beets 可能存相对路径 → 多 root 用 settings.to_abs() 解析
    from app.config import get_settings  # noqa: PLC0415
    _sx = get_settings()

    def _abs(p_str: str) -> str:
        if not p_str:
            return ""
        pp = Path(p_str)
        if not pp.is_absolute():
            pp = _sx.to_abs(pp)
        return str(pp)

    by_dir: dict[str, dict] = defaultdict(lambda: {
        "rgs": set(), "artist_mbids": set(),
        "album": "", "artist": "",
        "bound": 0, "total": 0,
    })
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """SELECT mb_releasegroupid, mb_artistid, mb_albumartistid,
                          path, album, artist
                   FROM beets.items"""
            )
        ).all()
    for r in rows:
        p = _abs(_decode(r.path))
        if not p:
            continue
        d = _os.path.dirname(p)
        slot = by_dir[d]
        slot["total"] += 1
        if r.mb_releasegroupid:
            slot["bound"] += 1
            slot["rgs"].add(r.mb_releasegroupid)
            am = (r.mb_albumartistid or r.mb_artistid or "").strip()
            if am:
                slot["artist_mbids"].add(am)
        if not slot["album"] and r.album:
            slot["album"] = r.album
        if not slot["artist"] and r.artist:
            slot["artist"] = r.artist

    written = 0
    skipped_mixed = 0
    skipped_partial = 0
    skipped_unbound = 0
    skipped_nodir = 0

    for d, slot in by_dir.items():
        if slot["bound"] == 0:
            skipped_unbound += 1
            continue
        if len(slot["rgs"]) != 1:
            skipped_mixed += 1
            continue
        # 必须 dir 下所有 items 都绑了, 否则说明还没稳态
        if slot["bound"] != slot["total"]:
            skipped_partial += 1
            continue
        dpath = Path(d)
        if not dpath.is_dir():
            skipped_nodir += 1
            continue
        rg = next(iter(slot["rgs"]))
        fields: dict = {"rg_mbid": rg}
        if len(slot["artist_mbids"]) == 1:
            fields["artist_mbid"] = next(iter(slot["artist_mbids"]))
        if slot["album"]:
            fields["_album"] = slot["album"]
        if slot["artist"]:
            fields["_artist"] = slot["artist"]
        if info_sidecar.write(dpath, fields):
            written += 1
    log.info(
        "sync_sidecars: %d dirs total | sidecar 写 %d | 跳过 mixed=%d partial=%d unbound=%d nodir=%d",
        len(by_dir), written, skipped_mixed, skipped_partial, skipped_unbound, skipped_nodir,
    )
