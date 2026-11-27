"""收件箱整理 endpoints —— 提议 + 审批拷进 Library."""

from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import APIRouter
from pydantic import BaseModel

from app import artist_alias, beets_bridge, inbox, inbox_cleanup
from app.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/proposals")
async def proposals() -> dict:
    """扫收件箱 → 按专辑产出整理提议 (不动任何文件). 扫描+md5 较重, 丢线程跑."""
    return await asyncio.to_thread(inbox.compute_proposals)


@router.get("/library")
async def library() -> dict:
    """列 Library/ 里已整理的专辑 (右栏 '整理好的')."""
    return await asyncio.to_thread(inbox.list_library)


class IdentifyPayload(BaseModel):
    srcs: list[str] | None = None


@router.post("/identify")
async def identify(payload: IdentifyPayload | None = None) -> dict:
    """对指定文件 (某目录未匹配的几首) 联网跑 fpcalc + AcoustID. 完后再 GET proposals."""
    srcs = payload.srcs if payload else None
    return await inbox.identify_unmatched(srcs)


class ExtractPayload(BaseModel):
    path: str


@router.post("/extract")
async def extract(payload: ExtractPayload) -> dict:
    """前处理: 解压一个压缩档 (zip/rar/7z). 解完音频会被下次扫描捡到."""
    return await asyncio.to_thread(inbox.extract_archive, payload.path)


class CueSplitPayload(BaseModel):
    cue: str
    audio: str


@router.post("/cue-split")
async def cue_split(payload: CueSplitPayload) -> dict:
    """前处理: 把大文件 + CUE 拆成单曲. 拆完进匹配流程."""
    return await asyncio.to_thread(inbox.split_cue, payload.cue, payload.audio)


class ApproveTrack(BaseModel):
    src: str
    disc: int = 1
    track: int = 0
    title: str = ""


class ApprovePayload(BaseModel):
    rg_mbid: str = ""
    album: str = ""
    album_artist: str = ""
    song_srcs: list[str]
    tracks: list[ApproveTrack] | None = None     # 逐曲 碟/轨/标题 (本地解析, 用户可改)
    year: str = ""
    artist_aliases: list[str] | None = None      # 目录里出现过的歌手写法 → 学进别名表
    upgrade_mode: str = "skip"                   # 库里有同曲但新来的更好: skip / replace / keep


def _lib():
    s = get_settings()
    return beets_bridge.get_library(s.beets_db, s.music_root)


@router.post("/approve")
async def approve(payload: ApprovePayload) -> dict:
    """把一个目录拷进目标专辑 + import 进 beets. 源文件不动."""
    def _run() -> dict:
        return inbox.approve(
            payload.rg_mbid, payload.album, payload.album_artist,
            payload.song_srcs, _lib(),
            tracks=[t.model_dump() for t in payload.tracks] if payload.tracks else None,
            year=payload.year, artist_aliases=payload.artist_aliases,
            upgrade_mode=payload.upgrade_mode,
        )

    return await asyncio.to_thread(_run)


@router.post("/approve-high")
async def approve_high() -> dict:
    """一键批准: 最近一次提议里所有高置信、未进库的目录 (按本地解析结果)."""
    return await asyncio.to_thread(lambda: inbox.approve_all_high(_lib()))


class ConfirmPayload(BaseModel):
    ids: list[str] | None = None


@router.post("/confirm")
async def confirm(payload: ConfirmPayload | None = None) -> dict:
    """后台联网确认 (第 3 层): 歌手别名 + 在歌手自己的专辑里按时长找对应 MB 专辑.
    立即返回, 进度看 GET /confirm; 跑完重新 GET proposals 即可看到结果."""
    if not inbox.confirm_state()["running"]:
        ids = payload.ids if payload else None
        threading.Thread(target=inbox.run_confirm, args=(ids,), daemon=True).start()
    return inbox.confirm_state()


@router.get("/confirm")
async def confirm_status() -> dict:
    return inbox.confirm_state()


@router.get("/library-merge")
async def library_merge_plan() -> dict:
    """Library 里分裂的歌手 (`动力火车`/`動力火車`) + 错绑 mbid 的合并计划 (只读)."""
    return await asyncio.to_thread(lambda: artist_alias.plan_library_merge(_lib()))


@router.post("/library-merge")
async def library_merge_apply() -> dict:
    return await asyncio.to_thread(lambda: artist_alias.apply_library_merge(_lib()))


# ── 清理: 整理完的原始文件 → 回收站 (可恢复) ─────────────────────
@router.get("/cleanup")
async def cleanup_report() -> dict:
    """整理文件夹每个顶层条目: 可安全删除 / 还有没入库的 (列出来) / 拷贝中."""
    return await asyncio.to_thread(inbox_cleanup.cleanup_report)


class CleanupPayload(BaseModel):
    paths: list[str]


@router.post("/cleanup")
async def cleanup_apply(payload: CleanupPayload) -> dict:
    """移到回收站 (移动前重新核实, 只动可删的)."""
    return await asyncio.to_thread(inbox_cleanup.trash_entries, payload.paths)


@router.get("/trash")
async def trash_list() -> dict:
    return {"items": await asyncio.to_thread(inbox_cleanup.list_trash)}


class RestorePayload(BaseModel):
    id: str


@router.post("/trash/restore")
async def trash_restore(payload: RestorePayload) -> dict:
    return await asyncio.to_thread(inbox_cleanup.restore, payload.id)


# ── 原生整理 App 用 ──────────────────────────────────────────────
@router.get("/config")
async def inbox_config() -> dict:
    """整理文件夹 / Library 位置 (App 要监听整理文件夹变化、在 Finder 里打开)."""
    s = get_settings()
    return {
        "source_roots": [str(p) for p in s.all_source_roots],
        "library_root": str(s.music_root),
        "allow_file_writes": s.allow_file_writes,
        "trash_retention_days": s.trash_retention_days,
    }


@router.get("/preview")
async def preview(src: str, start: float = 0.0):
    """试听整理文件夹里的一个文件: ffmpeg 转成 mp3 流 (APE/WavPack 等系统播不了的格式用).
    只允许读整理文件夹里的文件。"""
    import subprocess  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from fastapi import HTTPException  # noqa: PLC0415
    from fastapi.responses import StreamingResponse  # noqa: PLC0415

    p = Path(src).resolve()
    roots = [r.resolve() for r in get_settings().all_source_roots]
    if not p.is_file() or not any(r in p.parents for r in roots):
        raise HTTPException(404, "not in inbox")
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(max(start, 0.0)),
         "-i", str(p), "-t", "90", "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "pipe:1"],
        stdout=subprocess.PIPE,
    )

    def _gen():
        try:
            while chunk := proc.stdout.read(64 * 1024):
                yield chunk
        finally:
            proc.kill()

    return StreamingResponse(_gen(), media_type="audio/mpeg")
