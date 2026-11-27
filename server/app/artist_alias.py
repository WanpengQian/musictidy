"""歌手别名表 (整理第 2 层) —— `张惠妹 = 張惠妹 = Amei = A-mei = A-mei Cheung = aMEI`.

表 artist_alias (musictidy.db): 归一化别名 key → (规范名, artist_mbid, 来源)。
- 规范名 = Library 里要用的显示名。同一 mbid 的所有别名共享同一个规范名。
- key 用 folder_parse.norm_key (简繁/大小写/标点无关), 所以 `动力火车`/`動力火車`
  本来就同 key; 别名表解决的是跨语言/罗马字这类 key 对不上的写法。

种子来源:
- 离线: mb_artist 缓存 (name + sort_name) + Library 里 beets 的 albumartist/mbid
- 联网: MB artist aliases (按 mbid 拉) / 按名字搜 MB (只收别名里真能对上的)
- 审批即学习: 用户批准时, 解析出的各种写法都记成最终歌手的别名 (learn)

parse_folder 通过 index().key 做歌手聚类 → `Amei`(目录名) 和 `張惠妹`(tag) 同一票。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db import get_engine
from app.folder_parse import norm_key

log = logging.getLogger(__name__)

VA_NAMES = {"variousartists", "va", "群星", "合辑", "合輯", "various"}


def _ensure_table(conn) -> None:
    conn.execute(text(
        """CREATE TABLE IF NOT EXISTS artist_alias (
               alias_key   TEXT PRIMARY KEY,
               alias       TEXT NOT NULL,
               canonical   TEXT NOT NULL,
               artist_mbid TEXT NOT NULL DEFAULT '',
               source      TEXT NOT NULL DEFAULT '',
               updated_at  INTEGER NOT NULL
           )"""
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_artist_alias_mbid ON artist_alias(artist_mbid)"
    ))


# 来源优先级: 用户亲手定的 > Library 现状 > MB > 学到的
_SOURCE_RANK = {"user": 4, "library": 3, "mb": 2, "mb-alias": 2, "learned": 1}

# `A-mei II` (第二张专辑) / `Beyond 1` 这种尾巴
_TAIL = re.compile(r"\s+(?:[IVX]{1,4}|\d{1,2})$")
# MB sort-name `Nakashima, Mika` → `Mika Nakashima`
_SORT_NAME = re.compile(r"^([^,]+),\s*([^,]+)$")


@dataclass
class AliasIndex:
    by_key: dict[str, tuple[str, str]]          # alias_key → (canonical, mbid)

    def resolve(self, name: str | None) -> tuple[str, str] | None:
        """→ (规范名, mbid) 或 None。认不出时试着去掉尾部罗马数字/序号再查。"""
        if not name:
            return None
        k = norm_key(name)
        if not k:
            return None
        hit = self.by_key.get(k)
        if hit is None:
            stripped = _TAIL.sub("", name.strip())
            if stripped != name.strip():
                hit = self.by_key.get(norm_key(stripped))
        return hit

    def key(self, name: str) -> str:
        """给 folder_parse 用的歌手聚类 key: 认得的归到 mbid/规范名, 认不得退回 norm_key。"""
        hit = self.resolve(name)
        if hit:
            canon, mbid = hit
            return f"mb:{mbid}" if mbid else norm_key(canon)
        return norm_key(name)

    def canonical(self, name: str) -> str:
        hit = self.resolve(name)
        return hit[0] if hit else name


_lock = threading.Lock()
_cache: AliasIndex | None = None


def _invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def index() -> AliasIndex:
    """内存索引 (写入后自动失效重建)。表空时先离线播种一次。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
    with get_engine().begin() as conn:
        _ensure_table(conn)
        n = conn.execute(text("SELECT COUNT(*) FROM artist_alias")).scalar() or 0
    if not n:
        seed_offline()
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT alias_key, canonical, artist_mbid FROM artist_alias")
        ).fetchall()
    idx = AliasIndex({r[0]: (r[1], r[2] or "") for r in rows})
    with _lock:
        _cache = idx
    return idx


def _upsert(conn, alias: str, canonical: str, mbid: str, source: str) -> bool:
    """写一条别名; 已有且来源优先级更高的不覆盖。返回是否写入。"""
    k = norm_key(alias)
    if not k or not canonical:
        return False
    row = conn.execute(
        text("SELECT source, artist_mbid FROM artist_alias WHERE alias_key=:k"), {"k": k},
    ).first()
    if row:
        if _SOURCE_RANK.get(row[0], 0) > _SOURCE_RANK.get(source, 0):
            return False
        # 已挂到另一个 mbid 的别名, 低优先级来源不许改绑 (同名不同人)
        if row[1] and mbid and row[1] != mbid and _SOURCE_RANK.get(source, 0) < 4:
            return False
    conn.execute(
        text(
            """INSERT INTO artist_alias (alias_key, alias, canonical, artist_mbid, source, updated_at)
               VALUES (:k, :a, :c, :m, :s, :t)
               ON CONFLICT(alias_key) DO UPDATE SET
                   alias=:a, canonical=:c,
                   artist_mbid=CASE WHEN :m != '' THEN :m ELSE artist_mbid END,
                   source=:s, updated_at=:t"""
        ),
        {"k": k, "a": alias, "c": canonical, "m": mbid or "", "s": source, "t": int(time.time())},
    )
    return True


def _canonical_for_mbid(conn, mbid: str) -> str | None:
    row = conn.execute(
        text(
            """SELECT canonical FROM artist_alias WHERE artist_mbid=:m
               ORDER BY CASE source WHEN 'user' THEN 0 WHEN 'library' THEN 1 ELSE 2 END
               LIMIT 1"""
        ),
        {"m": mbid},
    ).first()
    return row[0] if row else None


def _sort_name_variants(sort_name: str) -> list[str]:
    out = [sort_name]
    m = _SORT_NAME.match(sort_name.strip())
    if m:
        out.append(f"{m.group(2).strip()} {m.group(1).strip()}")
    return out


# ── 播种 ────────────────────────────────────────────────────────
def seed_offline() -> dict[str, int]:
    """离线播种: Library 现状 (beets albumartist + mbid) 优先定规范名, 再补 mb_artist 缓存。"""
    from app import beets_bridge  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    pairs: Counter[tuple[str, str]] = Counter()          # (名字, beets 上的 mbid) → 曲数
    try:
        lib = beets_bridge.get_library(s.beets_db, s.music_root)
        for it in lib.items():
            name = (it.albumartist or it.artist or "").strip()
            if name and norm_key(name) not in VA_NAMES:
                pairs[(name, it.mb_albumartistid or "")] += 1
    except Exception as e:  # noqa: BLE001
        log.warning("artist_alias seed: 读 Library 失败: %s", e)

    added = 0
    suspicious: list[str] = []
    with get_engine().begin() as conn:
        _ensure_table(conn)
        mb_names: dict[str, set[str]] = defaultdict(set)
        for mbid, name, sort_name in conn.execute(
            text("SELECT mbid, name, sort_name FROM mb_artist")
        ).fetchall():
            for a in [name, *_sort_name_variants(sort_name or "")]:
                if a:
                    mb_names[mbid].add(norm_key(a))
        # Library: 按名字 key 分组 (`动力火车`/`動力火車` 同组), 曲数最多的写法当规范名。
        # beets 上的 mbid 只有名字真对得上 mb_artist 才信 —— 防错绑 (Avicii 挂了中島美嘉的 mbid)。
        groups: dict[str, Counter[str]] = defaultdict(Counter)
        group_mbid: dict[str, Counter[str]] = defaultdict(Counter)
        # 只有名字对得上 MB 的 mbid 才挂; 对不上且该 mbid 属于 Library 里另一个歌手 = 错绑。
        lib_keys = {norm_key(name) for name, _ in pairs}
        for (name, mbid), n in pairs.items():
            k = norm_key(name)
            groups[k][name] += n
            if not mbid:
                continue
            if mbid not in mb_names or k in mb_names[mbid]:
                group_mbid[k][mbid] += n      # 名字对得上 / 缓存里没这 mbid 无从验证
            elif mb_names[mbid] & lib_keys:
                suspicious.append(f"{name} ↔ {mbid} (该 mbid 属于 Library 里的另一个歌手)")
            # 其余 (名字对不上但不冲突, 如 Yu Li-Na 挂了合作者陈钢): 不挂 mbid, 免得把陈钢认成她
        for k, names in groups.items():
            canon = names.most_common(1)[0][0]
            mbid = group_mbid[k].most_common(1)[0][0] if group_mbid[k] else ""
            for n in names:
                added += _upsert(conn, n, canon, mbid, "library")
        # mb_artist 缓存: name + sort_name (+ 倒装)
        for mbid, name, sort_name in conn.execute(
            text("SELECT mbid, name, sort_name FROM mb_artist")
        ).fetchall():
            canon = _canonical_for_mbid(conn, mbid) or name
            for a in [name, *_sort_name_variants(sort_name or "")]:
                if a:
                    added += _upsert(conn, a, canon, mbid, "mb")
    if suspicious:
        log.warning("artist_alias seed: Library 里名字和 mbid 对不上 (未采信): %s", suspicious)
    _invalidate()
    return {"added": added, "suspicious": suspicious}


def _mb_get_artist(mbid: str) -> dict[str, Any] | None:
    import musicbrainzngs  # noqa: PLC0415

    from app.workers.musicbrainz import _configure_user_agent  # noqa: PLC0415

    _configure_user_agent()
    try:
        return musicbrainzngs.get_artist_by_id(mbid, includes=["aliases"])["artist"]
    except Exception as e:  # noqa: BLE001
        log.info("artist_alias: MB artist %s: %s", mbid, e)
        return None


def fetch_mb_aliases(mbid: str) -> int:
    """联网拉某 mbid 的全部 MB 别名 (`A-Mei`, `张惠妹`, `Amei Chang` …) 挂到它的规范名下。"""
    a = _mb_get_artist(mbid)
    if not a:
        return 0
    names = [a.get("name", ""), *_sort_name_variants(a.get("sort-name", "") or "")]
    for al in a.get("alias-list") or []:
        names.append(al.get("alias", ""))
        if al.get("sort-name"):
            names.extend(_sort_name_variants(al["sort-name"]))
    added = 0
    with get_engine().begin() as conn:
        _ensure_table(conn)
        canon = _canonical_for_mbid(conn, mbid) or a.get("name", "")
        for n in names:
            if n:
                added += _upsert(conn, n, canon, mbid, "mb-alias")
    _invalidate()
    return added


def search_mb(name: str) -> tuple[str, str] | None:
    """按名字搜 MB 歌手; 只有名字/别名/sort-name 归一化后真能对上才收 (防同名误认)。
    成功则把该歌手的别名全拉进表, 返回 (规范名, mbid)。"""
    import musicbrainzngs  # noqa: PLC0415

    from app.workers.musicbrainz import _configure_user_agent  # noqa: PLC0415

    _configure_user_agent()
    k = norm_key(_TAIL.sub("", name.strip())) or norm_key(name)
    try:
        res = musicbrainzngs.search_artists(artist=name, limit=5)
    except Exception as e:  # noqa: BLE001
        log.info("artist_alias: MB search %r: %s", name, e)
        return None
    for cand in res.get("artist-list") or []:
        if int(cand.get("ext:score", 0)) < 70:
            continue
        mbid = cand["id"]
        a = _mb_get_artist(mbid)
        if not a:
            continue
        names = [a.get("name", ""), a.get("sort-name", "")]
        names += [x.get("alias", "") for x in a.get("alias-list") or []]
        names += [v for x in a.get("alias-list") or [] for v in _sort_name_variants(x.get("sort-name") or "")]
        names += _sort_name_variants(a.get("sort-name") or "")
        if k in {norm_key(n) for n in names if n}:
            fetch_mb_aliases(mbid)
            with get_engine().begin() as conn:
                _upsert(conn, name, _canonical_for_mbid(conn, mbid) or a["name"], mbid, "mb-alias")
            _invalidate()
            return index().resolve(name)
    return None


def enrich_online(names: list[str], limit: int = 30) -> dict[str, Any]:
    """整理前联网补别名: 认得 mbid 但没拉过别名的 → 拉别名; 完全不认得的 → 搜 MB。
    MB 限速 1 req/s (musicbrainzngs 自带节流), 所以按 limit 分批。"""
    idx = index()
    done_mbids: set[str] = set()
    with get_engine().connect() as conn:
        _ensure_table(conn)
        for (m,) in conn.execute(
            text("SELECT DISTINCT artist_mbid FROM artist_alias WHERE source='mb-alias'")
        ).fetchall():
            done_mbids.add(m)
    fetched, found, missed = 0, [], []
    for name in dict.fromkeys(n for n in names if n and norm_key(n) not in VA_NAMES):
        if fetched >= limit:
            break
        hit = idx.resolve(name)
        if hit and hit[1]:
            if hit[1] not in done_mbids:
                fetch_mb_aliases(hit[1])
                done_mbids.add(hit[1])
                fetched += 1
            continue
        fetched += 1
        r = search_mb(name)
        (found if r else missed).append(name)
        if r:
            done_mbids.add(r[1])
        idx = index()
    return {"fetched": fetched, "found": found, "missed": missed}


# ── 学习 / 手改 ─────────────────────────────────────────────────
def learn(aliases: list[str], canonical: str, mbid: str = "", source: str = "learned") -> None:
    """审批时: 这个目录里出现过的各种歌手写法 → 都记成最终歌手的别名。
    source='user' 表示用户在界面上手动改过 (优先级最高, 可改绑)。"""
    if not canonical or norm_key(canonical) in VA_NAMES:
        return
    with get_engine().begin() as conn:
        _ensure_table(conn)
        if mbid:
            canonical = _canonical_for_mbid(conn, mbid) or canonical
        for a in {canonical, *aliases}:
            if a and not a.isdigit():
                _upsert(conn, a, canonical, mbid, source)
    _invalidate()


def set_canonical(mbid_or_name: str, new_name: str) -> int:
    """把某歌手的规范名改成 new_name (所有别名一起改)。返回改了几行。"""
    with get_engine().begin() as conn:
        _ensure_table(conn)
        if re.fullmatch(r"[0-9a-f-]{36}", mbid_or_name):
            r = conn.execute(
                text("UPDATE artist_alias SET canonical=:c, updated_at=:t WHERE artist_mbid=:m"),
                {"c": new_name, "t": int(time.time()), "m": mbid_or_name},
            )
        else:
            old = conn.execute(
                text("SELECT canonical FROM artist_alias WHERE alias_key=:k"),
                {"k": norm_key(mbid_or_name)},
            ).first()
            if not old:
                return 0
            r = conn.execute(
                text("UPDATE artist_alias SET canonical=:c, updated_at=:t WHERE canonical=:o"),
                {"c": new_name, "t": int(time.time()), "o": old[0]},
            )
        m = conn.execute(
            text("SELECT artist_mbid FROM artist_alias WHERE canonical=:c AND artist_mbid!='' LIMIT 1"),
            {"c": new_name},
        ).first()
        _upsert(conn, new_name, new_name, m[0] if m else "", "user")
    _invalidate()
    return r.rowcount or 0


# ── Library 已分裂艺人合并 (`动力火车/` + `動力火車/` → 一个) ────────────
def plan_library_merge(lib) -> dict[str, Any]:
    """按别名表把 Library 顶层艺人目录归组; 同组多个目录 → 计划把专辑搬进规范名目录。
    同时找出 beets 上 albumartist / mbid 跟别名表不一致的曲目 (含错绑的 mbid)。只读。"""
    from app.config import get_settings  # noqa: PLC0415

    idx = index()
    root = get_settings().music_root
    groups: dict[str, list[Path]] = defaultdict(list)
    if root.exists():
        for d in sorted(root.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                groups[idx.key(d.name)].append(d)
    moves: list[dict[str, str]] = []
    conflicts: list[str] = []
    for dirs in groups.values():
        if len(dirs) < 2:
            continue
        canon = idx.canonical(dirs[0].name)
        target = root / canon
        for d in dirs:
            if d == target:
                continue
            for album in sorted(p for p in d.iterdir() if not p.name.startswith(".")):
                dst = target / album.name
                if dst.exists():
                    conflicts.append(f"{album} → {dst} 已存在, 跳过")
                else:
                    moves.append({"src": str(album), "dst": str(dst)})

    seeded = seed_offline()           # 重算一次错绑列表 (不改已有高优先级别名)
    bad_mbids = {s.split(" ↔ ")[1].split(" ")[0] for s in seeded.get("suspicious", [])}
    retag: list[dict[str, Any]] = []
    for it in lib.items():
        name = (it.albumartist or "").strip()
        hit = idx.resolve(name)
        if not hit:
            continue
        canon, mbid = hit
        new_mbid = it.mb_albumartistid or ""
        if mbid and new_mbid != mbid:
            new_mbid = mbid
        elif not mbid and new_mbid in bad_mbids:
            new_mbid = ""
        if canon != name or new_mbid != (it.mb_albumartistid or ""):
            retag.append({"id": it.id, "title": it.title, "albumartist": name,
                          "new_albumartist": canon, "mbid": it.mb_albumartistid or "",
                          "new_mbid": new_mbid})
    return {"moves": moves, "conflicts": conflicts, "retag": retag}


def apply_library_merge(lib) -> dict[str, Any]:
    """执行 plan: 搬专辑目录 + 同步 beets 路径 + 改 albumartist/mbid。需 ALLOW_FILE_WRITES。"""
    import shutil  # noqa: PLC0415

    from app.beets_bridge import _from_bytes, _to_bytes  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    if not get_settings().allow_file_writes:
        return {"ok": False, "reason": "ALLOW_FILE_WRITES=false"}
    plan = plan_library_merge(lib)
    moved = 0
    for mv in plan["moves"]:
        src, dst = Path(mv["src"]), Path(mv["dst"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved += 1
        for it in lib.items():
            p = _from_bytes(it.path)
            try:
                rel = p.relative_to(src)
            except ValueError:
                continue
            it.path = _to_bytes(dst / rel)
            it.store()
        try:
            src.parent.rmdir()          # 旧艺人目录空了就删
        except OSError:
            pass
    for r in plan["retag"]:
        for it in lib.items(f"id:{r['id']}"):
            it.albumartist = r["new_albumartist"]
            it.mb_albumartistid = r["new_mbid"]
            it.store()
    return {"ok": True, "moved": moved, "retagged": len(plan["retag"]),
            "conflicts": plan["conflicts"]}
