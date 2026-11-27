"""收件箱清理 —— 整理完的原始文件核实"有着落"后, 移进回收站 (可恢复, 30 天后清).

用户流程: 自己把原始文件拷进整理文件夹 (Music/) → 解析/审批入库 → 这里核实
"可安全删除" → 用户确认 → 移到 DATA_DIR/trash/inbox/。App 自己不往整理文件夹里
加东西, 也不会没经用户确认删东西 (源数据红线, ADR 003: 删除 = 软删, 不 rm)。

粒度 = 整理文件夹根下的每个顶层条目 (通常就是用户拖进来的那个文件夹)。
一个音频文件"有着落"当且仅当:
- 它的内容 (md5) 已在 Library (approve 时记的是源文件 md5, 转 FLAC 也认得)
- 或它跟某首已入库的歌同 (碟,轨) —— 是被淘汰的低格式副本 (mp3 被 flac 顶替)
- 或它是 CUE 整轨大文件, 拆出来的分轨全部有着落
压缩包必须已解压 (解出来的音频按上面规则算)。CUE/日志/封面/说明等附属文件不影响。
刚拷进来还在变化的 (mtime 很新) → 「拷贝中」, 不给删。
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from app import archive as _archive
from app import inbox
from app.config import get_settings
from app.workers.scan import AUDIO_EXTS

log = logging.getLogger(__name__)

SETTLE_SECONDS = 15          # 最近这么多秒内还在变 = 拷贝中
MAX_BLOCKERS = 30


def _trash_root() -> Path:
    return get_settings().trash_dir / "inbox"


def _entries(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if not p.name.startswith("."))


def _files(entry: Path) -> list[Path]:
    if entry.is_file():
        return [entry]
    return [p for p in entry.rglob("*")
            if p.is_file() and not any(x.startswith(".") for x in p.relative_to(entry).parts)]


def cleanup_report() -> dict[str, Any]:
    """整理文件夹里每个顶层条目的状态:
    deletable (全部有着落) / pending (还有没入库的, 列出 blockers) / copying / empty。"""
    s = get_settings()
    props = inbox.compute_proposals()
    lib_md5s = inbox._library_md5s()

    accounted: set[str] = set()
    song_of: dict[str, str] = {}               # 文件 → 所属目录的显示名 (blocker 提示用)
    for f in props["folders"]:
        for sg in f["songs"]:
            members = sg.get("members") or [sg["src"]]
            for m in members:
                song_of[m] = f["folder"]
            if sg.get("covered", sg["in_library"]):   # 已入库 / 库里已有同曲 / 用户选了保留库里的
                accounted.update(members)
    cue_images = {c["audio"]: c for c in props["cue_pairs"]}

    def _audio_ok(p: Path) -> bool:
        sp = str(p)
        if sp in accounted:
            return True
        m = inbox._md5(p)
        return bool(m and m in lib_md5s)

    now = time.time()
    out: list[dict[str, Any]] = []
    for root in s.all_source_roots:
        for entry in _entries(root):
            files = _files(entry)
            try:
                size = sum(f.stat().st_size for f in files)
                newest = max((f.stat().st_mtime for f in files), default=entry.stat().st_mtime)
            except OSError:
                size, newest = 0, now
            audio = [f for f in files if f.suffix.lower() in AUDIO_EXTS]
            archives = [f for f in files if f.suffix.lower() in _archive.ARCHIVE_EXTS]
            blockers: list[dict[str, str]] = []
            ok = 0
            for a in audio:
                sa = str(a.resolve())
                if sa in cue_images:
                    tracks = a.parent / f"{a.stem}_tracks"
                    split = [t for t in _files(tracks) if t.suffix.lower() in AUDIO_EXTS] \
                        if tracks.exists() else []
                    if split and all(_audio_ok(t.resolve()) for t in split):
                        ok += 1
                    else:
                        blockers.append({"file": str(a.relative_to(root)),
                                         "reason": "整轨未拆 / 分轨未全部入库"})
                    continue
                if _audio_ok(a.resolve()):
                    ok += 1
                else:
                    blockers.append({"file": str(a.relative_to(root)),
                                     "reason": "未入库", "folder": song_of.get(sa, "")})
            for arc in archives:
                if not _archive.is_already_extracted(arc):
                    blockers.append({"file": str(arc.relative_to(root)), "reason": "压缩包未解压"})
            if now - newest < SETTLE_SECONDS:
                status = "copying"
            elif blockers:
                status = "pending"
            elif not audio and not archives:
                status = "empty"
            else:
                status = "deletable"
            out.append({
                "path": str(entry), "name": entry.name, "is_dir": entry.is_dir(),
                "size_mb": round(size / (1024 * 1024), 1),
                "audio_total": len(audio), "accounted": ok,
                "status": status,
                "blockers": blockers[:MAX_BLOCKERS], "blocker_count": len(blockers),
            })
    purge_expired()
    return {
        "entries": out,
        # 一键清理只算 deletable; empty (没音乐, 可能是别的东西, 如 Music.app 资料库) 要单独确认
        "deletable": sum(1 for e in out if e["status"] == "deletable"),
        "trash_retention_days": s.trash_retention_days,
    }


def trash_entries(paths: list[str]) -> dict[str, Any]:
    """把可删的顶层条目移进回收站。移动前重新核实, 只动 deletable/empty 的。"""
    s = get_settings()
    if not s.allow_file_writes:
        return {"ok": False, "reason": "ALLOW_FILE_WRITES=false (.env 改 true 才允许写)"}
    roots = [r.resolve() for r in s.all_source_roots]
    report = {e["path"]: e for e in cleanup_report()["entries"]}
    moved, refused = [], []
    for sp in paths:
        p = Path(sp)
        if p.resolve().parent not in roots:
            refused.append({"path": sp, "reason": "不是整理文件夹里的顶层条目"})
            continue
        e = report.get(str(p))
        if not e or e["status"] not in ("deletable", "empty"):
            refused.append({"path": sp, "reason": f"不能删: {e['status'] if e else '不存在'}",
                            "blockers": (e or {}).get("blockers", [])[:5]})
            continue
        tid = f"{time.strftime('%Y%m%d-%H%M%S')}__{p.name}"
        dst = _trash_root() / tid
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(p), str(dst))
        except OSError as ex:
            refused.append({"path": sp, "reason": repr(ex)})
            continue
        (_trash_root() / f"{tid}.json").write_text(json.dumps({
            "id": tid, "original": str(p), "trashed_at": int(time.time()),
            "size_mb": e["size_mb"], "audio_total": e["audio_total"],
        }, ensure_ascii=False), encoding="utf-8")
        moved.append({"path": sp, "id": tid})
    return {"ok": not refused, "moved": moved, "refused": refused}


def list_trash() -> list[dict[str, Any]]:
    root = _trash_root()
    if not root.exists():
        return []
    out = []
    for mf in sorted(root.glob("*.json"), reverse=True):
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta["exists"] = (root / meta["id"]).exists()
        out.append(meta)
    return out


def restore(tid: str) -> dict[str, Any]:
    """从回收站放回原位置 (原位置已有同名东西则拒绝, 不覆盖)。"""
    root = _trash_root()
    mf = root / f"{tid}.json"
    if "/" in tid or not mf.exists():
        return {"ok": False, "reason": "回收站里没有这一项"}
    meta = json.loads(mf.read_text(encoding="utf-8"))
    src, dst = root / tid, Path(meta["original"])
    if not src.exists():
        return {"ok": False, "reason": "回收站里的文件已不在"}
    if dst.exists():
        return {"ok": False, "reason": f"原位置已有同名: {dst}"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    mf.unlink()
    return {"ok": True, "path": str(dst)}


def purge_expired() -> int:
    """回收站里超过保留期的真删 (ADR 003)。"""
    s = get_settings()
    cutoff = time.time() - s.trash_retention_days * 86400
    n = 0
    for meta in list_trash():
        if meta.get("trashed_at", 0) < cutoff:
            p = _trash_root() / meta["id"]
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
            (_trash_root() / f"{meta['id']}.json").unlink(missing_ok=True)
            n += 1
    return n
