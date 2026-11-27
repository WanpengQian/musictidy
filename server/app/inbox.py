"""收件箱整理 —— 扫 Music/ → 匹配 → 按专辑分组提议 → 审批后拷进 Library.

模型 (见 memory: musictidy-inbox-library-model):
- Music/ (source_roots) = 收件箱, 用户拷批次进来; 只读, 绝不改。
- Library/ (music_root)  = 正规库, 每 recording 一个最优副本; server 只伺候这里。

流程 (结构优先, 见 docs/organizer-todo.md):
1. 扫 source_roots 所有音频 → 按物理专辑目录分组 (折叠 CD1/APE/_tracks 子目录)
2. 第 1 层 folder_parse: 目录名/tag/CUE/日志/说明文件/文件名投票 → 歌手/专辑/逐曲 碟轨标题 + 置信度
3. 第 2 层 artist_alias: 歌手聚类 + 规范名 (`Amei`=`張惠妹`, `动力火车`→`動力火車`)
4. 第 3 层 album_confirm: 联网在歌手自己的专辑里按时长序列确认 (后台跑, 结果缓存; 这里只读缓存)
5. 第 4 层 指纹: 本地解析不是 high 时, AcoustID 候选作为备选 (low 时排前面)
6. 同 (碟,轨) 多格式只留最优; md5 已在库的标「已整理」
7. 审批: 拷进 Library/$albumartist/$album/[Disc N/]NN. title.ext + import 进 beets;
   高置信目录可一键批量批准。歌手写法学进别名表

拷贝不移动 (源数据红线): Music 里的文件全程不动, 用户事后自己删剩下的垃圾。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app import album_confirm as _album_confirm
from app import album_dates as _album_dates
from app import archive as _archive
from app import artist_alias as _artist_alias
from app import beets_bridge, fingerprint_db
from app import cuesplit as _cuesplit
from app import folder_parse as _folder_parse
from app import library_match as _library_match
from app.config import get_settings
from app.db import get_engine
from app.workers.scan import walk_audio_files

log = logging.getLogger(__name__)

# 格式优先级 (按扩展名, 越大越优): 无损 > 有损; 同档比文件大小。
# 注: .m4a 可能是 ALAC(无损) 或 AAC(有损), 不读 tag 无法区分, 保守按有损算。
FORMAT_RANK = {
    "flac": 100, "ape": 92, "wv": 90, "wav": 85, "aiff": 84, "aif": 84,
    "m4a": 50, "aac": 50, "ogg": 45, "opus": 45, "mp3": 40,
}

# md5 缓存: (path, size, mtime) → md5, 避免每次 GET 重算 (批次小, 但稳)
_md5_cache: dict[tuple[str, int, float], str] = {}


# ── 数据模型 ────────────────────────────────────────────────────
@dataclass
class ProposedTrack:
    disc: int
    position: int
    title: str
    recording_mbid: str
    src: str
    src_rel: str
    dst: str
    dst_rel: str
    fmt: str
    size_mb: float
    md5: str
    alternatives: list[dict[str, Any]] = field(default_factory=list)  # 被 best 淘汰的同 recording 其他格式
    dst_exists: bool = False


@dataclass
class AlbumProposal:
    id: str                       # 物理专辑目录 (唯一标识, 审批按它走)
    rg_mbid: str                  # 专辑身份 (多数票选出的 MB release group; 封面/元数据用)
    album: str
    album_artist: str
    src_dir_rel: str              # 收件箱里的目录名 (UI 显示"哪个文件夹")
    canonical_total: int          # MB 该专辑总曲数 (tracks_json 长度)
    tracks: list[ProposedTrack] = field(default_factory=list)
    # 应有曲目列表 (手风琴展开用): 每个 canonical slot + 是否已落位
    canonical: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── helpers ─────────────────────────────────────────────────────
# 纯格式名的子目录, 归并时折叠回父级 (APE/FLAC 副本跟 MP3 是同一张盘)
FORMAT_DIRS = {
    "ape", "flac", "mp3", "wav", "wv", "wavpack", "alac",
    "m4a", "aac", "ogg", "opus", "aiff", "aif", "dsd", "dsf",
}


# 碟号子目录: CD1 / CD 2 / DISC3 / DISK 1 / Disc1 …
_DISC_DIR_RE = re.compile(r"^(?:cd|disc|disk)\s*0*(\d+)$", re.IGNORECASE)


def _album_dir(p: Path) -> Path:
    """文件所属的物理专辑目录: 一路折叠 格式子目录(APE/FLAC) / 拆轨子目录(xxx_tracks)
    / 碟号子目录(CD1/DISC2) 回到真正的专辑目录。多 CD = 一张专辑。"""
    d = p.parent
    while d != d.parent:
        n = d.name.lower()
        if d.name.endswith("_tracks") or n in FORMAT_DIRS or _DISC_DIR_RE.match(n):
            d = d.parent
        else:
            break
    return d


def _disc_of(p: Path) -> int:
    """从路径里的 CD1/DISC2 段取碟号; 没有则第 1 碟。取最靠近文件的那段。"""
    for part in reversed(p.parts):
        m = _DISC_DIR_RE.match(part.lower())
        if m:
            return int(m.group(1))
    return 1


def _ensure_lib_md5_table(conn) -> None:
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS library_content (md5 TEXT PRIMARY KEY)"
    ))


def _library_md5s() -> set[str]:
    """已整理进 Library 的内容 md5 集合 —— 查持久化表 (瞬时), 绝不每次重哈希整库。

    approve 拷入时记录每个文件的 md5; 表空时 (老库/首次) 一次性回填现有 Library,
    之后永远走表。这样刷新 / approve 都是 O(1) 查表, 不随库增大变慢。
    """
    s = get_settings()
    with get_engine().begin() as conn:
        _ensure_lib_md5_table(conn)
        out = {r[0] for r in conn.execute(text("SELECT md5 FROM library_content")).fetchall() if r[0]}
        if out:
            return out
        # 表空 → 一次性回填 (仅这一次哈希现有 Library)
        for root in s.all_library_roots:
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower().lstrip(".") in FORMAT_RANK:
                    m = _md5(p)
                    if m:
                        out.add(m)
        for m in out:
            conn.execute(
                text("INSERT OR IGNORE INTO library_content (md5) VALUES (:m)"),
                {"m": m},
            )
    return out


def _ensure_dismissed_table(conn) -> None:
    conn.execute(text(
        """CREATE TABLE IF NOT EXISTS inbox_dismissed (
               md5    TEXT PRIMARY KEY,
               reason TEXT NOT NULL DEFAULT '',
               at     INTEGER NOT NULL
           )"""
    ))


def _dismissed_md5s() -> set[str]:
    """用户明确说"保留库里那份, 这份不要"的收件箱文件 —— 算有着落 (可清理), 不再提示替换。"""
    with get_engine().begin() as conn:
        _ensure_dismissed_table(conn)
        return {r[0] for r in conn.execute(text("SELECT md5 FROM inbox_dismissed")).fetchall()}


def _dismiss(md5s: list[str], reason: str) -> None:
    import time as _t  # noqa: PLC0415

    with get_engine().begin() as conn:
        _ensure_dismissed_table(conn)
        for m in md5s:
            conn.execute(
                text("INSERT OR REPLACE INTO inbox_dismissed (md5, reason, at) VALUES (:m, :r, :t)"),
                {"m": m, "r": reason, "t": int(_t.time())},
            )


def _record_library_md5s(md5s: list[str]) -> None:
    """approve 拷入后, 把这些 md5 记进表 (后续判重直接命中, 不用再哈希)。"""
    md5s = [m for m in md5s if m]
    if not md5s:
        return
    with get_engine().begin() as conn:
        _ensure_lib_md5_table(conn)
        for m in md5s:
            conn.execute(
                text("INSERT OR IGNORE INTO library_content (md5) VALUES (:m)"),
                {"m": m},
            )


_ILLEGAL = re.compile(r'[/\x00-\x1f<>:"\\|?*]')
# 源文件名开头的轨号: "04 ...", "04. ...", "Track No04"
_LEADING_NUM = re.compile(r'^\s*(?:track\s*no\.?\s*)?(\d{1,3})\b', re.IGNORECASE)
# "碟-轨" 格式: "1-13 ..." → 碟1 轨13 (常见多 CD rip)
_DISC_TRACK = re.compile(r'^\s*(\d{1,2})\s*-\s*(\d{1,3})\b')


def _src_track_no(name: str) -> int:
    m = _DISC_TRACK.match(name)
    if m:
        return int(m.group(2))   # 取 "-" 后面的轨号, 别把碟号当轨号
    m = _LEADING_NUM.match(name)
    return int(m.group(1)) if m else 0


# 无损非 FLAC → 统一转 FLAC (bit-perfect, 兼容性好、压缩好)。有损不动 (重编码掉质量)。
LOSSLESS_TO_FLAC = {"ape", "wav", "wv", "wavpack", "aiff", "aif"}


def _copy_or_flac(src: Path, dst: Path, fmt: str) -> None:
    """无损非 FLAC → ffmpeg 转 FLAC (无损); 其余原样拷。dst 已带正确扩展名。"""
    if fmt in LOSSLESS_TO_FLAC:
        import subprocess  # noqa: PLC0415
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(src), "-c:a", "flac", "-compression_level", "8", str(dst)],
            check=True,
        )
    else:
        shutil.copy2(str(src), str(dst))


def _clean_title(stem: str) -> str:
    """从文件名 stem 抠出干净标题: 去开头轨号 '06. ' / '06 - ' / '06、'。"""
    t = re.sub(r"^\s*\d{1,3}\s*[.\-、_]?\s*", "", stem).strip()
    return t or stem


def _safe(name: str, fallback: str = "Unknown") -> str:
    name = (name or "").strip()
    name = _ILLEGAL.sub("_", name).strip(". ")
    return name or fallback


def _ensure_md5_table(conn) -> None:
    conn.execute(text(
        """CREATE TABLE IF NOT EXISTS file_md5 (
               path  TEXT PRIMARY KEY,
               size  INTEGER NOT NULL,
               mtime REAL NOT NULL,
               md5   TEXT NOT NULL
           )"""
    ))


def _md5(path: Path) -> str | None:
    """文件 md5, 两级缓存: L1 内存 + L2 持久化表 (file_md5, 按 path+size+mtime)。
    持久化让首次哈希后 —— 哪怕服务器重启 —— 都不再重算, 大批量收件箱秒扫。"""
    try:
        st = path.stat()
    except OSError:
        return None
    sp = str(path)
    key = (sp, st.st_size, st.st_mtime)
    cached = _md5_cache.get(key)
    if cached:
        return cached
    # L2: 持久化表
    with get_engine().begin() as conn:
        _ensure_md5_table(conn)
        row = conn.execute(
            text("SELECT md5 FROM file_md5 WHERE path=:p AND size=:s AND mtime=:t"),
            {"p": sp, "s": st.st_size, "t": st.st_mtime},
        ).first()
    if row and row[0]:
        _md5_cache[key] = row[0]
        return row[0]
    # 算 + 落两级缓存
    m = fingerprint_db.compute_md5(path)
    if m:
        _md5_cache[key] = m
        with get_engine().begin() as conn:
            _ensure_md5_table(conn)
            conn.execute(
                text(
                    """INSERT INTO file_md5 (path, size, mtime, md5)
                       VALUES (:p, :s, :t, :m)
                       ON CONFLICT(path) DO UPDATE SET
                           size=:s, mtime=:t, md5=:m"""
                ),
                {"p": sp, "s": st.st_size, "t": st.st_mtime, "m": m},
            )
    return m


def _load_rg(conn, rg_mbid: str) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            "SELECT title, artist_name, tracks_json FROM mb_release_group WHERE mbid = :m"
        ),
        {"m": rg_mbid},
    ).first()
    if not row:
        return None
    tracks = []
    if row.tracks_json:
        try:
            tracks = json.loads(row.tracks_json)
        except (ValueError, TypeError):
            tracks = []
    return {"title": row.title, "artist_name": row.artist_name, "tracks": tracks}


# ── 扫描 + 匹配 ─────────────────────────────────────────────────
@dataclass
class _Scanned:
    path: Path
    md5: str
    rec: str
    rg: str
    title: str
    artist: str
    album: str
    candidate_rgs: list[str]      # 该录音可能所属的多个 release group


def _scan_and_match(
    skip: set[Path] | None = None,
) -> tuple[list[_Scanned], list[tuple[Path, str]], list[Path]]:
    """扫 source_roots, 返回 (命中, 未命中[(path,md5)], 重复忽略).
    skip: 要跳过的文件 (如 CUE 大文件 —— 它由拆轨处理, 不当单曲)。"""
    s = get_settings()
    skip = skip or set()
    seen_md5: set[str] = set()
    matched: list[_Scanned] = []
    unmatched: list[tuple[Path, str]] = []
    dup_ignored: list[Path] = []

    for root in s.all_source_roots:
        for path in walk_audio_files(root):
            if path in skip:
                continue
            md5 = _md5(path)
            if not md5:
                continue
            if md5 in seen_md5:
                dup_ignored.append(path)        # byte-identical, 已有代表
                continue
            seen_md5.add(md5)
            hit = fingerprint_db.lookup_by_md5(md5)
            if not hit or not hit.get("recording_mbid"):
                unmatched.append((path, md5))
                continue
            cands = list(hit.get("candidate_rgs") or [])
            rg = hit.get("release_group_mbid") or ""
            if rg and rg not in cands:
                cands.insert(0, rg)        # 主 rg 也算候选之一
            matched.append(_Scanned(
                path=path, md5=md5,
                rec=hit["recording_mbid"], rg=rg,
                title=hit.get("title") or "", artist=hit.get("artist") or "",
                album=hit.get("album") or "",
                candidate_rgs=cands,
            ))
    return matched, unmatched, dup_ignored


# 一个目录里最多探查多少个候选 rg 的元数据 (按票数取前 N, 防一首歌 23 候选全查)
_MAX_CANDIDATES_PROBE = 15


def _rank_scanned(sc: _Scanned) -> tuple[int, int]:
    ext = sc.path.suffix.lower().lstrip(".")
    try:
        size = sc.path.stat().st_size
    except OSError:
        size = 0
    return (FORMAT_RANK.get(ext, 10), size)



# ── 本地解析 → 提议 ─────────────────────────────────────────────
# 上次 compute_proposals 的结果 (id → folder + 解析), 批量批准 / 联网确认用
_last_folders: dict[str, dict[str, Any]] = {}


def _build_folder(
    conn, album_dir: Path, folder_id: str, folder_label: str,
    files: list[dict[str, Any]], pt_by_path: dict[str, Any], pa: Any,
    conf: dict[str, Any] | None, lib_md5s: set[str],
    lib_index: Any = None, dismissed: set[str] | None = None,
) -> dict[str, Any]:
    """一个解析结果 (ParsedAlbum) + 它的文件 → 前端 folder:
    曲目按 (碟,轨) 去重只留最优格式; 候选 = 本地解析 (首位) + 指纹候选 (第 4 层兜底)。"""
    # 同一首 (碟,轨) 多格式只留最优; 碟/轨来自解析器 (认得 `最红 CD2_tracks`)。
    # 无损版继承匹配那一份的 MB 身份。
    slots: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for fi in files:
        pt = pt_by_path.get(str(fi["path"]))
        disc, no = (pt.disc, pt.track) if pt else (fi["disc"], fi["track"])
        title = (pt.title if pt else "") or fi["title"]
        if no > 0:
            k: Any = ("t", disc, no)
        elif title:
            k = ("n", _folder_parse.norm_key(title))
        else:
            k = ("p", str(fi["path"]))
        slots[k].append({**fi, "disc": disc, "track": no, "ptitle": title,
                         "tsrc": pt.title_source if pt else ""})

    fsongs: list[dict[str, Any]] = []
    matched_fis: list[dict[str, Any]] = []
    for group in slots.values():
        best = max(group, key=lambda x: (
            FORMAT_RANK.get(x["fmt"], 10),
            x["path"].stat().st_size if x["path"].exists() else 0,
        ))
        mf = next((g for g in group if g["rec"]), None)
        cands: list[str] = []
        for g in group:
            for c in g["cands"]:
                if c and c not in cands:
                    cands.append(c)
        try:
            size_mb = round(best["path"].stat().st_size / (1024 * 1024), 1)
        except OSError:
            size_mb = 0.0
        fsongs.append({
            "key": best["md5"], "src": str(best["path"]), "name": best["path"].name,
            "disc": best["disc"], "track": best["track"],
            "title": best["ptitle"] or (mf["title"] if mf else ""),
            "title_source": best["tsrc"] or ("acoustid" if mf else ""),
            "rec_mbid": mf["rec"] if mf else "", "fmt": best["fmt"], "size_mb": size_mb,
            "in_library": best["md5"] in lib_md5s, "matched": bool(mf),
            "alts": len(group) - 1,
            # 同一首的所有文件 (含被淘汰的低格式): 最优版入库 = 这些都有着落 (清理核实用)
            "members": [str(g["path"]) for g in group],
        })
        if mf:
            mf = dict(mf)
            mf["cands"] = cands
            matched_fis.append(mf)
    fsongs.sort(key=lambda x: (x["disc"], x["track"] or 999, x["name"]))

    # 跟 Library 按专辑/曲目比: have (库里一样好或更好) / upgrade (新来的更好) / new (库里缺)
    conf_rg = (conf or {}).get("rg_mbid", "") if (conf or {}).get("status") == "confirmed" else ""
    lib_album = lib_index.match(pa.artist, pa.album, conf_rg) if (lib_index and pa.album) else None
    dismissed = dismissed or set()
    for x in fsongs:
        lt = lib_album.find(x["disc"], x["track"], x["title"]) if lib_album else None
        st = "have" if x["in_library"] else _library_match.compare(x["fmt"], lt)
        if st == "upgrade" and x["key"] in dismissed:
            st = "kept"                   # 用户选了保留库里那份
        x["lib_status"] = st
        x["lib_fmt"] = lt.fmt if lt else ""
        x["covered"] = x["in_library"] or st in ("have", "kept")
    in_lib_count = sum(1 for x in fsongs if x["covered"])
    upgrades = sum(1 for x in fsongs if x["lib_status"] == "upgrade")

    # 年份候选: 本地 (tag/说明文件/目录名) + 联网 (MB 首发/该版本/Wikidata), 用户在 App 里挑
    dates: list[dict[str, str]] = []
    for d in list(pa.dates) + list((conf or {}).get("dates") or []):
        if d.get("date") and not any(x["date"] == d["date"] and x["source"] == d["source"] for x in dates):
            dates.append(d)
    default_date = _album_dates.pick_default(dates) or pa.year
    if default_date:
        pa.year = default_date           # 批量批准也用它

    # 候选 1: 本地解析 (确认过的话带 MB rg)
    candidates: list[dict[str, Any]] = []
    if pa.album and pa.artist:
        candidates.append({
            "key": "local",
            "source": "local",
            # 库里已有这张 → 用库里的名字/身份, 补进同一张, 不另起一张
            "rg_mbid": (lib_album.rg if lib_album and lib_album.rg else conf_rg),
            "album": lib_album.album if lib_album else pa.album,
            "album_artist": lib_album.artist if lib_album else pa.artist,
            "year": pa.year,
            "confidence": pa.confidence,
            "canonical_total": len(fsongs),
            "fit": len(fsongs),
            "votes": 0,
            "canonical": [
                {"disc": x["disc"], "position": x["track"], "title": x["title"],
                 "recording_mbid": x["rec_mbid"]}
                for x in fsongs
            ],
        })
    # 候选 2+: 指纹 (第 4 层) —— 本地高置信时不要, 省得干扰
    if pa.confidence != "high":
        fp = _fingerprint_candidates(conn, album_dir, matched_fis)
        if pa.confidence == "low" and any(c["fit"] > 0 for c in fp):
            candidates = fp + candidates      # 本地解析不出来 → 指纹优先
        else:
            candidates += fp

    return {
        "id": folder_id,
        "folder": folder_label,
        "song_count": len(fsongs),
        "in_library_count": in_lib_count,
        "organized": in_lib_count == len(fsongs) and len(fsongs) > 0,
        "songs": fsongs,
        "candidates": candidates,
        "library_match": {
            **lib_album.summary(),
            "have": sum(1 for x in fsongs if x["lib_status"] in ("have", "kept")),
            "upgrade": upgrades,
            "new": sum(1 for x in fsongs if x["lib_status"] == "new"),
        } if lib_album else None,
        "needs_decision": upgrades > 0,
        "parsed": {
            "kind": pa.kind, "artist": pa.artist, "album": pa.album, "year": pa.year,
            "confidence": pa.confidence, "notes": pa.notes,
            "dates": dates,
            "artist_votes": pa.artist_votes, "album_votes": pa.album_votes,
            "confirm": {k: (conf or {}).get(k) for k in ("status", "method", "score", "title")}
            if conf else None,
        },
        "guess_album": pa.album or (candidates[0]["album"] if candidates else album_dir.name),
        "guess_artist": pa.artist or (candidates[0]["album_artist"] if candidates else ""),
    }


def _fingerprint_candidates(conn, album_dir: Path, matched_fis: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """指纹候选 (旧逻辑): 每首的 candidate_rgs 投票, 按目录内吻合曲数排。"""
    folder_recs = {mf["rec"] for mf in matched_fis}
    folder_artist = next((mf["artist"] for mf in matched_fis if mf["artist"]), "")
    rg_votes: Counter[str] = Counter()
    for mf in matched_fis:
        for rg in mf["cands"]:
            if rg:
                rg_votes[rg] += 1
    out: list[dict[str, Any]] = []
    for rg, votes in rg_votes.most_common(_MAX_CANDIDATES_PROBE):
        meta = _load_rg(conn, rg)
        if not meta:
            continue
        canon = meta["tracks"]
        canon_recs = {t.get("recording_mbid") for t in canon if t.get("recording_mbid")}
        out.append({
            "key": rg, "source": "acoustid",
            "rg_mbid": rg,
            "album": meta["title"] or "Unknown Album",
            "album_artist": meta["artist_name"] or folder_artist or "Unknown Artist",
            "canonical_total": len(canon),
            "fit": len(folder_recs & canon_recs),
            "votes": votes,
            "canonical": [
                {"disc": int(t.get("disc") or 1), "position": int(t.get("position") or 0),
                 "title": t.get("title") or "", "recording_mbid": t.get("recording_mbid") or ""}
                for t in sorted(canon, key=lambda x: (int(x.get("disc") or 1), int(x.get("position") or 0)))
            ],
        })
    present = {c["rg_mbid"] for c in out}
    by_prim: dict[str, dict[str, Any]] = {}
    for mf in matched_fis:
        krg = mf["rg"] or ""
        g = by_prim.setdefault(krg, {"album": "", "artist": "", "n": 0})
        g["n"] += 1
        g["album"] = g["album"] or mf["album"]
        g["artist"] = g["artist"] or mf["artist"]
    for krg, g in by_prim.items():
        if krg and krg in present:
            continue
        out.append({
            "key": f"fp:{krg}:{g['album']}", "source": "acoustid",
            "rg_mbid": krg,
            "album": g["album"] or album_dir.name or "Unknown Album",
            "album_artist": g["artist"] or folder_artist or "Unknown Artist",
            "canonical_total": 0, "fit": g["n"], "votes": g["n"], "canonical": [],
            "no_meta": True,
        })
    out.sort(key=lambda c: (c["fit"], c["votes"]), reverse=True)
    return out

# ── 提议: 按目录 + 每目录候选专辑 ──────────────────────────────
def compute_proposals() -> dict[str, Any]:
    """扫收件箱 → 按物理目录分组, 每个目录给出候选专辑 (按吻合度排)。

    交互模型: 左栏点一个目录 → 中栏出这个目录的候选专辑名 (可能多个) →
    用户把目录拖到选中那张 → 确认 = 拷贝。不动任何文件。
    """
    s = get_settings()
    src_roots = s.all_source_roots

    def _dir_rel(d: Path) -> str:
        rel = None
        for r in src_roots:
            try:
                rel = d.relative_to(r)
                break
            except ValueError:
                continue
        if rel is None:
            return d.name
        # 解压产物在 .../_extracted/<专辑>/... 里, 显示时砍到 _extracted 之后那段,
        # 不然目录名又长又丑 (动力火车/_extracted/动力火车1997…[WAV+CUE]/…_tracks)
        parts = rel.parts
        if "_extracted" in parts:
            i = len(parts) - 1 - parts[::-1].index("_extracted")
            parts = parts[i + 1:]
        return "/".join(parts) if parts else d.name

    # 先探 CUE 大文件: 它们由"拆轨"处理, 不当单曲扫 (否则整张大 WAV 会混进来)
    cue_pairs: list[dict[str, Any]] = []
    cue_audio_skip: set[Path] = set()
    for root in src_roots:
        for cue, audio in _cuesplit.detect_pairs(root):
            cue_audio_skip.add(audio.resolve())
            try:
                size_mb = round(audio.stat().st_size / (1024 * 1024), 1)
            except OSError:
                size_mb = 0.0
            tracks_dir = audio.parent / f"{audio.stem}_tracks"
            split_done = tracks_dir.exists() and any(tracks_dir.iterdir())
            cue_pairs.append({
                "cue": str(cue), "audio": str(audio),
                "name": audio.name, "folder": _dir_rel(audio.parent),
                "size_mb": size_mb, "split": split_done,
            })
    cue_pairs.sort(key=lambda c: (c["folder"], c["name"]))

    matched, unmatched, dup_ignored = _scan_and_match(cue_audio_skip)
    lib_md5s = _library_md5s()   # 已整理进 Library 的内容 (md5 判重)

    # 把每个文件归到物理专辑目录 (匹配的 + 未匹配的都进)
    def _fmt_of(p: Path) -> str:
        return p.suffix.lower().lstrip(".")

    by_dir: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sc in matched:
        by_dir[str(_album_dir(sc.path))].append({
            "path": sc.path, "md5": sc.md5, "fmt": _fmt_of(sc.path),
            "disc": _disc_of(sc.path), "track": _src_track_no(sc.path.name),
            "title": sc.title or _clean_title(sc.path.stem),
            "rec": sc.rec, "rg": sc.rg, "artist": sc.artist, "album": sc.album,
            "cands": sc.candidate_rgs,
        })
    for p, md5 in unmatched:
        by_dir[str(_album_dir(p))].append({
            "path": p, "md5": md5, "fmt": _fmt_of(p),
            "disc": _disc_of(p), "track": _src_track_no(p.name),
            "title": _clean_title(p.stem),
            "rec": "", "rg": "", "artist": "", "album": "", "cands": [],
        })

    folders: list[dict[str, Any]] = []
    alias_idx = _artist_alias.index()
    new_last: dict[str, dict[str, Any]] = {}   # 建完整再一次性替换 (App 轮询刷新时别让别人读到半截)
    # Library 按专辑/曲目的索引: 同一张专辑换了个文件 (另一版 / mp3 / 改过 tag) 也认得
    lib_index = _library_match.build_index(
        beets_bridge.get_library(s.beets_db, s.music_root), alias_idx.key,
    )
    dismissed = _dismissed_md5s()

    def _root_of(d: Path) -> Path | None:
        return next((r for r in src_roots if r == d or r in d.parents), None)

    with get_engine().connect() as conn:
        for dir_str, files in by_dir.items():
            album_dir = Path(dir_str)
            fi_by_path = {str(fi["path"]): fi for fi in files}
            # 第 1+2 层: 本地结构解析 (目录名/tag/CUE/日志/文件名投票, 歌手走别名表)
            parsed_list = _folder_parse.parse_folder(
                album_dir, [fi["path"] for fi in files], _root_of(album_dir),
                artist_key=alias_idx.key, artist_canon=alias_idx.canonical,
            )
            for pi, pa in enumerate(parsed_list):
                # 第 3 层: 之前联网确认过的结果 (只读缓存, 不联网)
                conf = _album_confirm.cached_result(pa)
                if conf:
                    _album_confirm.apply_confirmation(pa, conf)
                pt_by_path = {t.path: t for t in pa.tracks}
                sub = [fi_by_path[t.path] for t in pa.tracks if t.path in fi_by_path]
                folder = _build_folder(
                    conn, album_dir, dir_str if len(parsed_list) == 1 else f"{dir_str}#{pi}",
                    _dir_rel(album_dir) + (f" · {pa.album or '未分专辑'}" if len(parsed_list) > 1 else ""),
                    sub, pt_by_path, pa, conf, lib_md5s, lib_index, dismissed,
                )
                folders.append(folder)
                new_last[folder["id"]] = {"folder": folder, "parsed": pa}
    folders.sort(key=lambda f: f["folder"])
    global _last_folders
    _last_folders = new_last

    # 压缩档 (前处理: 用户在左栏点"解压", 解完音频进上面的匹配流程)
    archives: list[dict[str, Any]] = []
    for root in src_roots:
        for arc in _archive.detect_archives(root):
            try:
                size_mb = round(arc.stat().st_size / (1024 * 1024), 1)
            except OSError:
                size_mb = 0.0
            archives.append({
                "path": str(arc),
                "name": arc.name,
                "folder": _dir_rel(arc.parent),
                "size_mb": size_mb,
                "extracted": _archive.is_already_extracted(arc),
            })
    archives.sort(key=lambda a: (a["folder"], a["name"]))
    # cue_pairs 已在前面探好 (用于跳过大文件 + 左栏拆轨)

    return {
        "folders": folders,
        "archives": archives,
        "cue_pairs": cue_pairs,
        "unmatched": [str(p) for p in unmatched],
        "dup_ignored": [str(p) for p in dup_ignored],
        "stats": {
            "matched_recordings": sum(
                sum(1 for s in f["songs"] if s["matched"]) for f in folders
            ),
            "folders": len(folders),
            "archives": len(archives),
            "cue_pairs": len(cue_pairs),
            "unmatched": len(unmatched),
            "dup_ignored": len(dup_ignored),
        },
    }


# ── 前处理: 解压压缩档 ──────────────────────────────────────────
def extract_archive(path_str: str) -> dict[str, Any]:
    """解压一个压缩档 (zip/rar/7z) 到同目录 _extracted/, 解完音频会被下次扫描捡到。
    源压缩档不动 (worker 那套会移 trash, 这里前处理先保留, 用户自己删)。"""
    arc = Path(path_str)
    if not arc.exists():
        return {"ok": False, "reason": "文件不存在"}
    if arc.suffix.lower() not in _archive.ARCHIVE_EXTS:
        return {"ok": False, "reason": "不是压缩档"}
    if _archive.is_already_extracted(arc):
        return {"ok": True, "already": True, "dst": str(_archive.extraction_dst(arc))}
    if not _archive.unar_available():
        return {"ok": False, "reason": "缺 unar —— brew install unar"}
    bad = _archive.validate_magic(arc)
    if bad:
        return {"ok": False, "reason": bad}
    try:
        dst = _archive.extract(arc)
        return {"ok": True, "dst": str(dst)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": repr(e)}


def split_cue(cue_str: str, audio_str: str) -> dict[str, Any]:
    """前处理: 把大文件 (整张 APE/FLAC/WAV) 按 CUE 拆成单曲, 放到 <音频>_tracks/ 子目录,
    下次扫描就当独立曲目匹配。源大文件 + CUE 不动 (用户自己删)。"""
    s = get_settings()
    if not s.allow_file_writes:
        return {"ok": False, "reason": "ALLOW_FILE_WRITES=false (.env 改 true 才允许写)"}
    cue, audio = Path(cue_str), Path(audio_str)
    if not cue.exists() or not audio.exists():
        return {"ok": False, "reason": "CUE 或音频文件不存在"}
    out_dir = audio.parent / f"{audio.stem}_tracks"
    if out_dir.exists() and any(out_dir.iterdir()):
        return {"ok": True, "already": True, "tracks": sum(1 for _ in out_dir.iterdir())}
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        new_paths = _cuesplit.split_pair(cue, audio, out_dir)
        return {"ok": True, "tracks": len(new_paths)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": repr(e)}


# ── 批准 → 拷进 Library ─────────────────────────────────────────
def approve(rg_mbid: str, album: str, album_artist: str,
            song_srcs: list[str], lib, *,
            tracks: list[dict[str, Any]] | None = None,
            year: str = "", artist_aliases: list[str] | None = None,
            artist_mbid: str = "", upgrade_mode: str = "skip") -> dict[str, Any]:
    """把一个目录 (song_srcs) 拷进 Library 的某张专辑并写身份。源文件不动。

    tracks: 逐曲 {src, disc, track, title} (本地解析结果, 用户可能改过) —— 优先用来命名;
    没给就退回 MB canonical / 指纹 / 源文件名。
    歌手名走别名表归一 (`动力火车`→`動力火車`), 并把 artist_aliases (这个目录里出现过的
    各种写法) 学进别名表 —— 审批即学习。

    Library 里已有同一张专辑时: 拷进那张的目录、用它的名字/身份 (不另起一张), 并逐轨比:
    库里一样好或更好 → 跳过; 库里缺 → 补; 新来的更好 → 看 upgrade_mode:
    skip 先不动 / replace 替换 (旧文件进回收站) / keep 保留库里的 (这份记成已处理)。"""
    s = get_settings()
    if not s.allow_file_writes:
        return {"ok": False, "reason": "ALLOW_FILE_WRITES=false (.env 改 true 才允许写)"}
    if not song_srcs:
        return {"ok": False, "reason": "没有歌曲被分配到这张专辑"}

    lib_root = s.music_root
    by_src = {t["src"]: t for t in (tracks or []) if t.get("src")}
    # rg canonical: recording → (碟,轨,标题) 以及 (碟,轨) → recording
    pos_by_rec: dict[str, tuple[int, int, str]] = {}
    rec_by_pos: dict[tuple[int, int], str] = {}
    with get_engine().connect() as conn:
        meta = _load_rg(conn, rg_mbid) if rg_mbid else None
        if rg_mbid and not artist_mbid:
            row = conn.execute(
                text("SELECT artist_mbid FROM mb_release_group WHERE mbid = :m"),
                {"m": rg_mbid},
            ).first()
            if row:
                artist_mbid = row.artist_mbid or ""
    if meta:
        album = album or meta["title"]
        album_artist = album_artist or meta["artist_name"]
        for t in meta["tracks"]:
            d, p_ = int(t.get("disc") or 1), int(t.get("position") or 0)
            rmb = t.get("recording_mbid")
            if rmb:
                pos_by_rec[rmb] = (d, p_, t.get("title") or "")
                rec_by_pos[(d, p_)] = rmb

    # 歌手: 别名表归一 + 学习
    idx = _artist_alias.index()
    hit = idx.resolve(album_artist)
    if hit:
        album_artist = hit[0]
        artist_mbid = artist_mbid or hit[1]
    _artist_alias.learn(list(artist_aliases or []), album_artist, artist_mbid,
                        source="user" if artist_aliases is None else "learned")

    lib_album = _library_match.build_index(lib, idx.key).match(album_artist, album, rg_mbid)
    if lib_album:
        album, album_artist = lib_album.album, lib_album.artist
        rg_mbid = rg_mbid or lib_album.rg
        artist_mbid = artist_mbid or lib_album.artist_mbid
    safe_artist, safe_album = _safe(album_artist), _safe(album)

    def _disc(src: str) -> int:
        t = by_src.get(src)
        return int(t["disc"]) if t and t.get("disc") else _disc_of(Path(src))

    discs_present = {_disc(sp) for sp in song_srcs}
    multi_disc = len(discs_present) > 1 or (discs_present and max(discs_present) > 1)
    if lib_album and lib_album.multi_disc:
        multi_disc = True
    replaced = kept = 0
    lib_md5s = _library_md5s()    # md5 判重 (查表, 瞬时): 已在库的内容绝不重复拷
    new_md5s: list[str] = []
    copied: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for src in song_srcs:
        src_p = Path(src)
        try:
            if not src_p.exists():
                errors.append({"src": src, "reason": "源文件不存在"})
                continue
            md5 = _md5(src_p)
            if md5 and md5 in lib_md5s:
                copied.append({"dst": src_p.name, "skipped": "已在库 (md5)"})
                continue
            hit_fp = fingerprint_db.lookup_by_md5(md5) if md5 else None
            ut = by_src.get(src) or {}
            phys_disc = _disc(src)
            pos = int(ut.get("track") or 0)
            rec = (hit_fp or {}).get("recording_mbid") or ""
            if pos and (phys_disc, pos) in rec_by_pos:
                rec = rec_by_pos[(phys_disc, pos)]       # 位置对上 canonical → 用它的 recording
            _cd, cpos, ctitle = pos_by_rec.get(rec, (1, 0, ""))
            pos = pos or cpos or _src_track_no(src_p.name)
            # 标题: 用户/解析给的 > canonical > 指纹 > 文件名
            title = (ut.get("title") or ctitle or (hit_fp or {}).get("title")
                     or _clean_title(src_p.stem))
            ext = src_p.suffix.lower().lstrip(".")
            if lib_album:
                lt = lib_album.find(phys_disc, pos, title)
                st = _library_match.compare(ext, lt)
                if st == "have":
                    copied.append({"dst": src_p.name, "skipped": "库里已有 (一样好或更好)"})
                    continue
                if st == "upgrade":
                    if upgrade_mode == "keep":
                        if md5:
                            _dismiss([md5], "keep library version")
                        kept += 1
                        copied.append({"dst": src_p.name, "skipped": "保留库里的版本"})
                        continue
                    if upgrade_mode != "replace":
                        copied.append({"dst": src_p.name, "skipped": "有更好版本, 未替换"})
                        continue
                    _library_match.trash_library_file(lt, lib)   # 旧版本进回收站
                    replaced += 1
            out_ext = "flac" if ext in LOSSLESS_TO_FLAC else ext   # 无损非 FLAC → flac
            fname = f"{pos:02d}. {_safe(title)}.{out_ext}" if pos else f"{_safe(title)}.{out_ext}"
            album_root = lib_album.dir if lib_album else lib_root / safe_artist / safe_album
            dst_p = (album_root / f"Disc {phys_disc}" / fname) if multi_disc else (album_root / fname)
            if dst_p.exists():
                copied.append({"dst": str(dst_p), "skipped": "已存在"})
                continue
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            _copy_or_flac(src_p, dst_p, ext)   # 无损转 FLAC / 其余原样拷; 源不动
            if md5:
                lib_md5s.add(md5)
                new_md5s.append(md5)
            iid = beets_bridge.import_file(lib, dst_p)
            if iid is not None:
                beets_bridge.set_item_meta(
                    lib, iid,
                    title=title or None, album=album or None,
                    track=pos or None, disc=phys_disc if multi_disc else None,
                    track_mbid=rec or None,
                    releasegroup_mbid=rg_mbid or None,
                    album_artist_mbid=artist_mbid or None,
                    album_artist=album_artist or None,
                    year=int(year[:4]) if year[:4].isdigit() else None,
                    month=int(year[5:7]) if year[5:7].isdigit() else None,
                    day=int(year[8:10]) if year[8:10].isdigit() else None,
                )
            copied.append({"dst": str(dst_p), "item_id": str(iid) if iid else "?"})
        except Exception as e:  # noqa: BLE001
            errors.append({"src": src, "reason": repr(e)})

    _record_library_md5s(new_md5s)

    return {
        "ok": len(errors) == 0,
        "album": album,
        "album_artist": album_artist,
        "copied": len([c for c in copied if "item_id" in c]),
        "skipped": len([c for c in copied if "skipped" in c]),
        "replaced": replaced,
        "kept": kept,
        "into_existing": bool(lib_album),
        "errors": errors,
    }


def _raw_artist_names(pa: Any) -> list[str]:
    return [v["value"] for v in pa.artist_votes if v.get("value")]


def approve_folder(folder_id: str, lib, *, min_confidence: str = "high") -> dict[str, Any]:
    """按上次提议里的本地解析结果直接批准一个目录 (批量批准用)。"""
    entry = _last_folders.get(folder_id)
    if not entry:
        return {"ok": False, "reason": "目录不在最近一次提议里, 先刷新"}
    folder, pa = entry["folder"], entry["parsed"]
    rank = {"low": 0, "medium": 1, "high": 2}
    if rank.get(pa.confidence, 0) < rank[min_confidence]:
        return {"ok": False, "reason": f"置信度 {pa.confidence} 不够"}
    local = next((c for c in folder["candidates"] if c.get("source") == "local"), None)
    if not local:
        return {"ok": False, "reason": "没有本地解析结果"}
    songs = [x for x in folder["songs"] if not x["covered"]]
    if not songs:
        return {"ok": True, "copied": 0, "skipped": len(folder["songs"]), "album": pa.album}
    return approve(
        local["rg_mbid"], local["album"], local["album_artist"],
        [x["src"] for x in songs], lib,
        tracks=[{"src": x["src"], "disc": x["disc"], "track": x["track"], "title": x["title"]}
                for x in songs],
        year=pa.year, artist_aliases=_raw_artist_names(pa),
    )


def approve_all_high(lib) -> dict[str, Any]:
    """一键批准: 上次提议里所有 high 置信、还没进库的目录。"""
    results: dict[str, Any] = {}
    for fid, entry in list(_last_folders.items()):
        f, pa = entry["folder"], entry["parsed"]
        if pa.confidence != "high" or f["organized"] or f.get("needs_decision"):
            continue          # 有"新来的更好, 要不要替换"的目录等用户自己决定
        results[fid] = approve_folder(fid, lib)
    ok = sum(1 for r in results.values() if r.get("ok"))
    return {"ok": ok == len(results), "albums": len(results), "succeeded": ok,
            "copied": sum(r.get("copied", 0) for r in results.values()),
            "failed": {k: r.get("reason") or r.get("errors") for k, r in results.items() if not r.get("ok")}}


# ── 联网确认 (第 3 层, 后台跑) ──────────────────────────────────
_confirm_state: dict[str, Any] = {"running": False, "done": 0, "total": 0, "confirmed": 0,
                                  "probable": 0, "current": "", "error": ""}


def confirm_state() -> dict[str, Any]:
    return dict(_confirm_state)


def run_confirm(ids: list[str] | None = None) -> None:
    """后台: 先联网补歌手别名, 再逐个目录找 MB 对应专辑 (MB 1 req/s, 慢但只跑一次, 结果缓存)。"""
    if _confirm_state["running"]:
        return
    targets = [
        (fid, e["parsed"]) for fid, e in list(_last_folders.items())
        if (ids is None or fid in ids) and e["parsed"].kind == "album"
        and e["parsed"].artist and e["parsed"].album and not e["folder"]["organized"]
    ]
    _confirm_state.update(running=True, done=0, total=len(targets), confirmed=0,
                          probable=0, current="", error="")
    try:
        names = [v["value"] for _f, pa in targets for v in pa.artist_votes]
        _confirm_state["current"] = "歌手别名"
        _artist_alias.enrich_online(names)
        for _fid, pa in targets:
            _confirm_state["current"] = f"{pa.artist} / {pa.album}"
            try:
                res = _album_confirm.confirm(pa)
                if res["status"] in ("confirmed", "probable"):
                    _confirm_state[res["status"]] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("confirm %s: %s", pa.dir, e)
            _confirm_state["done"] += 1
    except Exception as e:  # noqa: BLE001
        _confirm_state["error"] = repr(e)
    finally:
        _confirm_state.update(running=False, current="")


# ── 联网识别未匹配 (fpcalc + AcoustID) ─────────────────────────
async def identify_unmatched(
    srcs: list[str] | None = None, limit: int = 300,
) -> dict[str, Any]:
    """对指定文件 (srcs, 一般是某个目录里未匹配的几首) 跑 fpcalc + AcoustID,
    识别结果存进指纹缓存 (带 md5)。需联网 + ACOUSTID_API_KEY。
    AcoustID 限速 3 req/s, 顺序跑, 一张专辑约 15-40s。
    srcs=None 时退回扫全部未匹配 (兼容)。"""
    from app.workers.fingerprint import _try_acoustid  # noqa: PLC0415

    s = get_settings()
    if not s.acoustid_api_key:
        return {"ok": False, "reason": "没配 ACOUSTID_API_KEY, 无法联网识别"}

    if srcs is not None:
        # 只识别这批 (跳过已能命中的)
        unmatched = []
        for sp in srcs:
            p = Path(sp)
            if not p.exists():
                continue
            md5 = _md5(p)
            if not md5:
                continue
            hit = fingerprint_db.lookup_by_md5(md5)
            if hit and hit.get("recording_mbid"):
                continue  # 已经能匹配了, 跳过
            unmatched.append((p, md5))
    else:
        # 跳过 CUE 大文件 (整张 WAV, 不该逐个算指纹)
        skip: set[Path] = set()
        for root in s.all_source_roots:
            for _cue, audio in _cuesplit.detect_pairs(root):
                skip.add(audio.resolve())
        _, unmatched, _ = _scan_and_match(skip)
    if not unmatched:
        return {"ok": True, "identified": 0, "failed": 0, "total": 0}

    with get_engine().connect() as conn:
        min_id = conn.execute(
            text("SELECT MIN(item_id) FROM track_fingerprint")
        ).scalar() or 0
    next_id = min(int(min_id), 0) - 1

    identified = 0
    failed = 0
    for path, md5 in unmatched[:limit]:
        try:
            fp, dur, match = await _try_acoustid(path, s.acoustid_api_key, "")
        except Exception as e:  # noqa: BLE001
            log.warning("inbox identify: %s — %s", path.name, e)
            failed += 1
            continue
        if match and match.get("rec_mbid"):
            fingerprint_db.save(
                item_id=next_id, fingerprint=fp or "", duration_s=float(dur or 0),
                recording_mbid=match["rec_mbid"],
                release_group_mbid=match.get("rg_mbid"),
                title=match.get("_rec_title"), artist=match.get("_rec_artist"),
                album=match.get("_rec_album"), source="acoustid-inbox",
                candidate_rgs=match.get("_candidate_rgs"), md5=md5,
            )
            next_id -= 1
            identified += 1
        else:
            failed += 1
    return {
        "ok": True, "identified": identified, "failed": failed,
        "total": len(unmatched),
    }


# ── 整理好的: 读 Library 目录 (右栏) ────────────────────────────
def list_library() -> dict[str, Any]:
    """列 Library/ 里已整理的专辑 (艺人/专辑/曲数), 直接读文件系统。"""
    s = get_settings()
    root = s.music_root
    albums: list[dict[str, Any]] = []
    if root.exists():
        for artist_dir in sorted(root.iterdir()):
            if not artist_dir.is_dir() or artist_dir.name.startswith("."):
                continue
            for album_dir in sorted(artist_dir.iterdir()):
                if not album_dir.is_dir() or album_dir.name.startswith("."):
                    continue
                n = sum(
                    1 for f in album_dir.iterdir()
                    if f.is_file() and f.suffix.lower().lstrip(".") in FORMAT_RANK
                )
                if n:
                    albums.append({
                        "artist": artist_dir.name,
                        "album": album_dir.name,
                        "tracks": n,
                    })
    return {"albums": albums, "count": len(albums)}
