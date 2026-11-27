"""收件箱 vs Library 按专辑/曲目比对 —— 同一张专辑换了个文件 (另一次抓轨 / mp3 版 / 改过 tag)
也要认出来, 不能只靠整文件 md5。

Library 里的专辑按 (歌手别名 key, 专辑 norm_key) 和 MB release group 建索引; 收件箱目录解析出的
歌手/专辑 (或确认过的 rg) 对上某张库里的专辑后, 逐轨 (碟,轨; 没轨号退回标题) 比:
- have    库里这一轨一样好或更好 → 不拷, 这份原始文件也算"有着落" (可清理)
- upgrade 新来的更好 (flac 对 mp3) → 用户确认才替换 (旧文件进回收站)
- new     库里这张专辑缺这一轨 → 正常拷进同一张专辑目录 (补全)
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.folder_parse import norm_key

# 质量档: 无损拷进来都会转成 FLAC, 所以无损一律同档; 有损按格式
LOSSLESS = {"flac", "ape", "wv", "wavpack", "wav", "aiff", "aif", "tta", "tak", "alac"}
LOSSY_RANK = {"m4a": 50, "aac": 50, "ogg": 45, "opus": 45, "mp3": 40, "wma": 30}
_DISC_DIR = re.compile(r"^(?:disc|cd|disk)\s*\d+$", re.IGNORECASE)


def quality(fmt: str) -> int:
    f = (fmt or "").lower().lstrip(".")
    return 100 if f in LOSSLESS else LOSSY_RANK.get(f, 10)


@dataclass
class LibTrack:
    item_id: int
    path: str
    disc: int
    track: int
    title: str
    fmt: str


@dataclass
class LibAlbum:
    artist: str
    album: str
    rg: str
    artist_mbid: str
    dir: Path
    tracks: dict[tuple[int, int], LibTrack] = field(default_factory=dict)
    by_title: dict[str, LibTrack] = field(default_factory=dict)
    multi_disc: bool = False

    def find(self, disc: int, track: int, title: str) -> LibTrack | None:
        if track:
            t = self.tracks.get((disc or 1, track))
            if t:
                return t
        return self.by_title.get(norm_key(title)) if title else None

    def summary(self) -> dict[str, Any]:
        return {"artist": self.artist, "album": self.album, "rg": self.rg,
                "dir": str(self.dir), "tracks": len(self.tracks) or len(self.by_title)}


class LibraryIndex:
    def __init__(self, artist_key: Callable[[str], str]):
        self.artist_key = artist_key
        self.by_key: dict[tuple[str, str], LibAlbum] = {}
        self.by_rg: dict[str, LibAlbum] = {}

    def match(self, artist: str, album: str, rg: str = "") -> LibAlbum | None:
        if rg and rg in self.by_rg:
            return self.by_rg[rg]
        if not artist or not album:
            return None
        return self.by_key.get((self.artist_key(artist), norm_key(album)))


def _decode(p: Any) -> str:
    return bytes(p).decode("utf-8", "replace") if isinstance(p, (bytes, memoryview)) else str(p or "")


def build_index(lib, artist_key: Callable[[str], str]) -> LibraryIndex:
    """从 beets 建索引。同一张专辑 = 同 (歌手 key, 专辑 key); 有 rg 的再挂 rg。"""
    idx = LibraryIndex(artist_key)
    for it in lib.items():
        artist = (it.albumartist or it.artist or "").strip()
        album = (it.album or "").strip()
        if not artist or not album:
            continue
        path = _decode(it.path)
        k = (artist_key(artist), norm_key(album))
        a = idx.by_key.get(k)
        if a is None:
            parent = Path(path).parent
            multi = bool(_DISC_DIR.match(parent.name))
            a = LibAlbum(artist=artist, album=album, rg=it.mb_releasegroupid or "",
                         artist_mbid=it.mb_albumartistid or "",
                         dir=parent.parent if multi else parent, multi_disc=multi)
            idx.by_key[k] = a
        a.rg = a.rg or (it.mb_releasegroupid or "")
        a.artist_mbid = a.artist_mbid or (it.mb_albumartistid or "")
        if _DISC_DIR.match(Path(path).parent.name):
            a.multi_disc = True
        t = LibTrack(item_id=int(it.id), path=path, disc=int(it.disc or 0) or 1,
                     track=int(it.track or 0), title=it.title or "",
                     fmt=Path(path).suffix.lower().lstrip("."))
        if t.track:
            cur = a.tracks.get((t.disc, t.track))
            if cur is None or quality(t.fmt) > quality(cur.fmt):
                a.tracks[(t.disc, t.track)] = t
        if t.title:
            a.by_title.setdefault(norm_key(t.title), t)
    for a in idx.by_key.values():
        if a.rg:
            idx.by_rg.setdefault(a.rg, a)
    return idx


def compare(fmt: str, lib_track: LibTrack | None) -> str:
    if lib_track is None:
        return "new"
    return "upgrade" if quality(fmt) > quality(lib_track.fmt) else "have"


# ── 替换: 旧版本进回收站 ─────────────────────────────────────────
def trash_library_file(t: LibTrack, lib) -> str:
    """把 Library 里被替换掉的旧文件移进回收站 (DATA_DIR/trash/library/) + 删 beets 条目。
    ADR 003: 不 rm; 旁边留一份记录 (原路径)。"""
    from app import beets_bridge  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    root = get_settings().trash_dir / "library"
    root.mkdir(parents=True, exist_ok=True)
    src = Path(t.path)
    tid = f"{time.strftime('%Y%m%d-%H%M%S')}__{t.item_id}__{src.name}"
    if src.exists():
        shutil.move(str(src), str(root / tid))
    (root / f"{tid}.json").write_text(json.dumps({
        "id": tid, "original": t.path, "trashed_at": int(time.time()),
        "reason": "replaced by better version",
    }, ensure_ascii=False), encoding="utf-8")
    beets_bridge.remove_item_by_id(lib, t.item_id)
    return tid
