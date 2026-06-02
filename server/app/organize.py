"""规范化目录/文件名 —— preview + apply.

设计：
- 用 beets 自带的 path template 引擎计算 dst（不重新发明）
- 按当前源目录分组，每组通常 = 一张专辑
- sidecar (cover.jpg / .cue / .log 等) 跟随同 dir 的音频文件
- preview 不动文件；apply 检查 ALLOW_FILE_WRITES + 用户确认
- 失败半途：当前 best-effort 报错并继续；下次再点会基于当前状态重新算
"""

from __future__ import annotations

import logging
import os
import shutil
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from beets.util import functemplate

from app import beets_bridge
from app.config import get_settings

log = logging.getLogger(__name__)

# Path template:
#   $albumartist/$album/$track. $title
# 注意：所有 item 都用同一个 template。beets 的 'singleton:1' 查询会把
# 我们这种「直接 Item.from_path 而没创建 Album 行」的所有 item 都判成
# singleton，把它们丢 Non-Album/，我们不想要这个。
PATH_FORMATS = [
    ("default", functemplate.Template("$albumartist/$album/$track. $title")),
]

SIDECAR_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".cue", ".log", ".nfo", ".m3u", ".m3u8", ".lrc", ".txt", ".pdf",
}

CONFIDENCE_LABEL = {
    "recording": "指纹级",
    "album": "专辑级",
    "artist": "仅艺人",
    "tag-only": "仅 tag",
    "mixed": "混合",
}


# ── 数据模型 ────────────────────────────────────────────────────
@dataclass
class Move:
    kind: str           # 'audio' | 'sidecar'
    src: str
    dst: str
    src_rel: str        # 相对 music_root，UI 用
    dst_rel: str
    noop: bool
    item_id: int | None = None
    format: str | None = None
    bitrate_kbps: int | None = None


@dataclass
class OrganizeGroup:
    src_dir: str
    src_dir_rel: str
    album_artist: str
    album: str
    items_count: int
    confidence: str
    confidence_label: str
    moves: list[Move] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)
    src_dir_will_be_empty: bool = False
    all_noop: bool = False    # 整组都已规范化

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ── 内部 helpers ────────────────────────────────────────────────
def _music_root(lib) -> Path:
    return Path(
        lib.directory.decode("utf-8") if isinstance(lib.directory, (bytes, memoryview))
        else lib.directory
    )


def _item_src(item, music_root: Path) -> Path:
    p = Path(os.fsdecode(item.path))
    if not p.is_absolute():
        p = music_root / p
    return p.resolve()


def _item_dst(item) -> Path:
    """beets 算的目标路径（绝对）."""
    dest_bytes = item.destination(path_formats=PATH_FORMATS)
    return Path(os.fsdecode(dest_bytes))


def _confidence(item) -> str:
    if getattr(item, "mb_trackid", ""):
        return "recording"
    if getattr(item, "mb_releasegroupid", ""):
        return "album"
    if getattr(item, "mb_albumartistid", ""):
        return "artist"
    return "tag-only"


def _can_organize(item) -> bool:
    """安全门：只组织有像样元数据的 item。

    需要：artist + album 至少一个非空 + 任一 MB 标识或 tag.
    """
    has_artist = bool(item.albumartist or item.artist)
    has_album = bool(item.album)
    has_mbid = bool(
        item.mb_trackid or item.mb_releasegroupid or item.mb_albumartistid
    )
    # tag-only 也允许（只要 tag 完整），但 UI 会标 confidence='tag-only'
    return has_artist and has_album


def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


# ── Preview ─────────────────────────────────────────────────────
def compute_preview(lib) -> list[OrganizeGroup]:
    """按当前源 dir 分组，算每个文件的 src→dst."""
    music_root = _music_root(lib)
    by_dir: dict[Path, list] = defaultdict(list)
    skipped = 0

    for item in lib.items():
        try:
            src = _item_src(item, music_root)
        except Exception:
            skipped += 1
            continue
        if not _can_organize(item):
            skipped += 1
            continue
        by_dir[src.parent].append(item)

    if skipped:
        log.info("organize.preview: 跳过 %d 个元数据不足的 item", skipped)

    groups: list[OrganizeGroup] = []
    for src_dir, items in by_dir.items():
        moves: list[Move] = []
        confidences: set[str] = set()
        for it in items:
            src = _item_src(it, music_root)
            try:
                dst = _item_dst(it)
            except Exception as e:
                log.warning("organize: 算 dst 失败 item %s: %s", it.id, e)
                continue
            moves.append(Move(
                kind="audio",
                src=str(src),
                dst=str(dst),
                src_rel=_rel(src, music_root),
                dst_rel=_rel(dst, music_root),
                noop=(src == dst),
                item_id=int(it.id),
                format=it.format,
                bitrate_kbps=int(it.bitrate / 1000) if it.bitrate else 0,
            ))
            confidences.add(_confidence(it))

        if not moves:
            continue

        # sidecars：同 src_dir 里的非音频文件
        audio_dsts = {Path(m.dst).parent for m in moves}
        if len(audio_dsts) == 1 and src_dir.exists():
            dst_dir = audio_dsts.pop()
            try:
                for f in sorted(src_dir.iterdir()):
                    if f.is_file() and f.suffix.lower() in SIDECAR_EXTS:
                        sidecar_dst = dst_dir / f.name
                        moves.append(Move(
                            kind="sidecar",
                            src=str(f),
                            dst=str(sidecar_dst),
                            src_rel=_rel(f, music_root),
                            dst_rel=_rel(sidecar_dst, music_root),
                            noop=(f == sidecar_dst),
                        ))
            except Exception as e:
                log.warning("organize: 列 sidecar 失败 %s: %s", src_dir, e)

        # 算 src_dir 残留
        all_srcs = {Path(m.src) for m in moves}
        leftovers: list[str] = []
        if src_dir.exists():
            try:
                for f in src_dir.iterdir():
                    if f.is_file() and f not in all_srcs:
                        leftovers.append(_rel(f, music_root))
            except Exception:
                pass
        src_dir_will_be_empty = (
            len(leftovers) == 0 and len(moves) > 0 and src_dir.exists()
        )

        # confidence
        if len(confidences) == 1:
            conf = next(iter(confidences))
        elif confidences:
            conf = "mixed"
        else:
            conf = "tag-only"

        first = items[0]
        groups.append(OrganizeGroup(
            src_dir=str(src_dir),
            src_dir_rel=_rel(src_dir, music_root),
            album_artist=first.albumartist or first.artist or "",
            album=first.album or "",
            items_count=len(items),
            confidence=conf,
            confidence_label=CONFIDENCE_LABEL.get(conf, conf),
            moves=moves,
            leftovers=leftovers,
            src_dir_will_be_empty=src_dir_will_be_empty,
            all_noop=all(m.noop for m in moves),
        ))

    # 排序：未规范的在前；同状态按 src 字典序
    groups.sort(key=lambda g: (g.all_noop, g.src_dir_rel))
    return groups


# ── Apply ──────────────────────────────────────────────────────
def apply_group(lib, src_dir: str) -> dict[str, Any]:
    """应用单组归档：mv 所有 src→dst + 同步 beets item.path + 删空 src_dir."""
    s = get_settings()
    if not s.allow_file_writes:
        return {"ok": False, "reason": "ALLOW_FILE_WRITES=false （.env 里改成 true 才允许 mv 文件）"}

    groups = compute_preview(lib)
    target = next((g for g in groups if g.src_dir == src_dir), None)
    if target is None:
        return {"ok": False, "reason": f"src_dir 不在 preview 列表里: {src_dir}"}

    moved: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    item_path_updates: list[tuple[int, str]] = []

    for m in target.moves:
        if m.noop:
            continue
        src_p = Path(m.src)
        dst_p = Path(m.dst)
        try:
            if not src_p.exists():
                errors.append({"src": m.src, "reason": "源文件不存在"})
                continue
            if dst_p.exists():
                errors.append({"src": m.src, "reason": f"目标已存在: {m.dst}"})
                continue
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_p), str(dst_p))
            moved.append({"src": m.src, "dst": m.dst, "kind": m.kind})
            if m.kind == "audio" and m.item_id is not None:
                item_path_updates.append((m.item_id, m.dst))
        except Exception as e:
            errors.append({"src": m.src, "reason": repr(e)})

    # 同步 beets item.path
    for item_id, new_path in item_path_updates:
        try:
            for it in lib.items(f"id:{item_id}"):
                it.path = os.fsencode(new_path)
                it.store()
                break
        except Exception as e:
            log.warning("organize: 更新 beets item %d path 失败: %s", item_id, e)

    # 清理空 src_dir
    if not errors and target.src_dir_will_be_empty:
        try:
            Path(target.src_dir).rmdir()
        except OSError:
            pass

    return {
        "ok": len(errors) == 0,
        "moved": len(moved),
        "errors": errors,
        "src_dir": src_dir,
    }
