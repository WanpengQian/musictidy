"""把 beets 当 Python 库用的薄封装.

设计原则：
- 只暴露我们实际用到的几个操作
- API 用 pathlib.Path（str），内部转 bytes 喂给 beets
- 不依赖 beets CLI config —— 显式传 db_path / music_root
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── 延迟 import：beets 启动重，且 unit test 里可以 mock 掉 ───────────
def _import_beets():
    from beets.library import Item, Library  # noqa: PLC0415

    return Item, Library


# ── 内部 helpers ────────────────────────────────────────────────
def _to_bytes(p: Path) -> bytes:
    return os.fsencode(str(p))


def _from_bytes(b: bytes) -> Path:
    return Path(os.fsdecode(b))


# ── 库句柄管理 ──────────────────────────────────────────────────
_lib: Any | None = None


def get_library(db_path: Path, music_root: Path) -> Any:
    """打开 / 复用 beets Library 句柄."""
    global _lib
    if _lib is None:
        _, Library = _import_beets()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _lib = Library(str(db_path), directory=str(music_root))
        log.info("beets library opened: db=%s music_root=%s", db_path, music_root)
    return _lib


def close_library() -> None:
    global _lib
    if _lib is not None:
        try:
            _lib._connection().close()
        except Exception:
            pass
        _lib = None


@contextmanager
def transaction(lib: Any):
    """beets Library.transaction() 包一层，让外部不用 import beets."""
    with lib.transaction():
        yield


# ── 查询 ────────────────────────────────────────────────────────
def all_known_paths(lib: Any) -> set[Path]:
    """已知的全部 item 文件 path，统一规范化为绝对路径.

    beets 内部对 library.directory 下的文件有时存相对路径有时绝对（
    取决于 import 时的状态），导致两次 scan 之间路径不一致 → 误报为新文件 → 重复。
    这里强制 absolute + resolve()，保证一致.
    """
    music_root = Path(
        lib.directory.decode("utf-8") if isinstance(lib.directory, (bytes, memoryview))
        else lib.directory
    )
    out: set[Path] = set()
    for item in lib.items():
        try:
            p = _from_bytes(item.path)
            if not p.is_absolute():
                p = music_root / p
            out.add(p.resolve())
        except Exception:
            pass
    return out


def dedupe_items_by_path(lib: Any) -> int:
    """合并 beets DB 里同一物理文件的多个 item（相对 vs 绝对路径不同被当成两个）.

    返回删掉的 item 数。保留每组里 id 最小的（最早 import 的）。
    """
    by_resolved: dict[Path, list] = {}
    music_root = Path(
        lib.directory.decode("utf-8") if isinstance(lib.directory, (bytes, memoryview))
        else lib.directory
    )
    for item in lib.items():
        try:
            p = _from_bytes(item.path)
            if not p.is_absolute():
                p = music_root / p
            p = p.resolve()
        except Exception:
            continue
        by_resolved.setdefault(p, []).append(item)

    removed = 0
    for group in by_resolved.values():
        if len(group) <= 1:
            continue
        group.sort(key=lambda it: int(it.id))
        # 把 mb_* 信息合并到 id 最小的那个（避免丢失识别）
        keeper = group[0]
        for other in group[1:]:
            for field in (
                "mb_trackid", "mb_albumid", "mb_artistid",
                "mb_albumartistid", "mb_releasegroupid",
            ):
                if not getattr(keeper, field) and getattr(other, field):
                    setattr(keeper, field, getattr(other, field))
            other.remove()
            removed += 1
        keeper.store()
    return removed


def count_items(lib: Any) -> int:
    return sum(1 for _ in lib.items())


def count_identified(lib: Any) -> int:
    """有任何 MB 标识（recording / release-group / album-artist）的 item 数."""
    return sum(
        1 for it in lib.items()
        if it.mb_trackid or it.mb_releasegroupid or it.mb_albumartistid
    )


def count_at_recording_level(lib: Any) -> int:
    """有 MB recording id（AcoustID 指纹匹配级别）的 item 数."""
    return sum(1 for it in lib.items() if it.mb_trackid)


def count_at_releasegroup_level(lib: Any) -> int:
    """到 release-group 级（专辑级别识别）的 item 数."""
    return sum(1 for it in lib.items() if it.mb_releasegroupid)


def count_at_artist_only(lib: Any) -> int:
    """只挂到 album-artist 但没有 release-group（艺人挂上了但不知具体专辑）."""
    return sum(
        1 for it in lib.items()
        if it.mb_albumartistid and not it.mb_releasegroupid
    )


def iter_unidentified(lib: Any) -> Iterator[int]:
    """yield 完全没识别（连艺人都没挂上）的 item id."""
    for it in lib.items():
        if not it.mb_trackid and not it.mb_releasegroupid and not it.mb_albumartistid:
            yield int(it.id)


def iter_unique_albumartist_mbids(lib: Any) -> set[str]:
    """已识别 item 们覆盖到的所有艺人 MBID（去重）."""
    out: set[str] = set()
    for it in lib.items():
        mbid = it.mb_albumartistid or it.mb_artistid
        if mbid:
            out.add(mbid)
    return out


# ── 写入 ────────────────────────────────────────────────────────
@dataclass(slots=True)
class ImportStats:
    scanned: int = 0
    added: int = 0
    skipped: int = 0
    failed: int = 0


def import_file(lib: Any, path: Path) -> int | None:
    """读 tags + add 到 beets DB。不动文件。返回 item id 或 None（失败）."""
    Item, _ = _import_beets()
    try:
        item = Item.from_path(_to_bytes(path))
        item.add(lib)
        return int(item.id)
    except Exception as e:
        log.warning("import_file failed: %s — %s", path, e)
        return None


def remove_item_by_id(lib: Any, item_id: int) -> bool:
    """从 beets DB 删条目。不删文件（文件层走 trash 流程）."""
    for it in lib.items(f"id:{item_id}"):
        it.remove()
        return True
    return False


def get_item_path(lib: Any, item_id: int) -> Path | None:
    """item id → 绝对文件 path（beets 存的可能是相对路径，统一规范化）."""
    music_root = Path(
        lib.directory.decode("utf-8") if isinstance(lib.directory, (bytes, memoryview))
        else lib.directory
    )
    for it in lib.items(f"id:{item_id}"):
        try:
            p = _from_bytes(it.path)
            if not p.is_absolute():
                p = music_root / p
            return p.resolve()
        except Exception:
            return None
    return None


def get_item_tags(lib: Any, item_id: int) -> dict[str, Any] | None:
    """读 item 现有的关键 tag。用于 tag-based MB 搜索 fallback."""
    for it in lib.items(f"id:{item_id}"):
        return {
            "title": it.title or "",
            "artist": it.artist or "",
            "albumartist": it.albumartist or "",
            "album": it.album or "",
            "track": int(it.track or 0),
            "year": int(it.year or 0),
            "length": float(it.length or 0.0),
        }
    return None


def set_mb_ids(
    lib: Any,
    item_id: int,
    *,
    track_mbid: str | None = None,
    releasegroup_mbid: str | None = None,
    artist_mbid: str | None = None,
    album_artist_mbid: str | None = None,
    album_artist: str | None = None,
) -> bool:
    """写回识别结果。只更非 None 字段；返回是否找到 item.

    album_artist 是 canonical 名字（用于覆盖 it.albumartist —— organize 的
    目录名靠这个字段，所以 identify/fingerprint 命中后必须强制覆盖，否则
    旧 tag 残留，目录永远不会被 organize 重命名。
    """
    for it in lib.items(f"id:{item_id}"):
        if track_mbid is not None:
            it.mb_trackid = track_mbid
        if releasegroup_mbid is not None:
            it.mb_releasegroupid = releasegroup_mbid
        if artist_mbid is not None:
            it.mb_artistid = artist_mbid
        if album_artist_mbid is not None:
            it.mb_albumartistid = album_artist_mbid
        if album_artist is not None:
            it.albumartist = album_artist
        it.store()
        return True
    return False


def set_item_meta(
    lib: Any,
    item_id: int,
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    track: int | None = None,
    track_mbid: str | None = None,
    releasegroup_mbid: str | None = None,
    artist_mbid: str | None = None,
    album_artist_mbid: str | None = None,
    album_artist: str | None = None,
) -> bool:
    """通用 metadata 写回。只动非 None 字段。

    album_artist 显式传时强制覆盖 it.albumartist（用于把目录名规范成 MB
    canonical 名）。不传则保持之前的"空才写"行为，避免误改用户手动设的值。
    """
    for it in lib.items(f"id:{item_id}"):
        if title is not None: it.title = title
        if artist is not None:
            it.artist = artist
            if album_artist is None:
                it.albumartist = it.albumartist or artist
        if album_artist is not None:
            it.albumartist = album_artist
        if album is not None: it.album = album
        if track is not None: it.track = track
        if track_mbid is not None: it.mb_trackid = track_mbid
        if releasegroup_mbid is not None: it.mb_releasegroupid = releasegroup_mbid
        if artist_mbid is not None: it.mb_artistid = artist_mbid
        if album_artist_mbid is not None: it.mb_albumartistid = album_artist_mbid
        it.store()
        return True
    return False


# ── Test helper —— 重置模块级 lib 句柄（pytest fixture 用）─────
def _reset_for_tests() -> None:
    global _lib
    if _lib is not None:
        try:
            _lib._connection().close()
        except Exception:
            pass
    _lib = None
