"""Wishlist endpoints —— 用户想要但还没拿到的专辑清单。

工作流：iOS Shazam 听到一首歌 → 拿到 MB recording mbid → 查 MB 找到所在
release-group → "加心愿单" 落库；以后用户找/扒/买到，scan worker 给 items
回填 mb_releasegroupid，匹配上心愿单的 fulfilled_at 自动写时间戳。

Phase 1：手动管理 CRUD + 显示 fulfilled 状态。自动 fulfill 在 worker 里
做（这个 endpoint 提供 internal helper 给 scan 调）。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import get_engine

router = APIRouter()


class WishlistItemIn(BaseModel):
    rg_mbid: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    artist: str = ""
    artist_mbid: str = ""
    source: str = "manual"
    source_recording_mbid: str = ""
    notes: str = ""


def _row_to_dict(row) -> dict:
    return {
        "rg_mbid": row.rg_mbid,
        "title": row.title,
        "artist": row.artist or "",
        "artist_mbid": row.artist_mbid or "",
        "source": row.source or "manual",
        "source_recording_mbid": row.source_recording_mbid or "",
        "notes": row.notes or "",
        "added_at": int(row.added_at),
        "fulfilled_at": int(row.fulfilled_at) if row.fulfilled_at else None,
    }


@router.get("/wishlist")
async def list_wishlist() -> dict:
    """列出心愿单。fulfilled 的（已经在库里了）排后面，wanted 排前。
    每个条目尝试关联 mb_release_group 表查 first_release_date / primary_type。"""
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """SELECT w.rg_mbid, w.title, w.artist, w.artist_mbid,
                          w.source, w.source_recording_mbid, w.notes,
                          w.added_at, w.fulfilled_at,
                          rg.first_release_date, rg.primary_type
                   FROM wishlist w
                   LEFT JOIN mb_release_group rg ON rg.mbid = w.rg_mbid
                   ORDER BY w.fulfilled_at IS NOT NULL,
                            w.added_at DESC"""
            )
        ).all()
    items = []
    for r in rows:
        d = _row_to_dict(r)
        d["first_release_date"] = getattr(r, "first_release_date", "") or ""
        d["primary_type"] = getattr(r, "primary_type", "") or ""
        items.append(d)
    return {"count": len(items), "items": items}


@router.post("/wishlist")
async def add_wishlist(payload: WishlistItemIn) -> dict:
    """加一项；同 rg_mbid 重复加不报错（idempotent），但 added_at 不会更新。"""
    now = int(time.time())
    with get_engine().begin() as conn:
        # 看是不是已经存在
        exists = conn.execute(
            text("SELECT 1 FROM wishlist WHERE rg_mbid=:m"),
            {"m": payload.rg_mbid},
        ).first()
        if exists:
            # 静默更新 source / notes / source_recording_mbid（如果给了非空值）—
            # 用户可能从两个不同入口加同一张，第二次的 notes 也保留下来
            updates = []
            params: dict = {"m": payload.rg_mbid}
            if payload.notes.strip():
                updates.append("notes=:n")
                params["n"] = payload.notes.strip()
            if payload.source_recording_mbid.strip():
                updates.append("source_recording_mbid=:r")
                params["r"] = payload.source_recording_mbid.strip()
            if updates:
                conn.execute(
                    text(f"UPDATE wishlist SET {', '.join(updates)} WHERE rg_mbid=:m"),
                    params,
                )
            return {"ok": True, "already_existed": True, "rg_mbid": payload.rg_mbid}

        # 如果当前 items 里已经有这张专辑了，直接 fulfilled 设 now
        already_owned = conn.execute(
            text(
                """SELECT 1 FROM beets.items
                   WHERE mb_releasegroupid=:m LIMIT 1"""
            ),
            {"m": payload.rg_mbid},
        ).first()
        fulfilled = now if already_owned else None

        conn.execute(
            text(
                """INSERT INTO wishlist
                   (rg_mbid, title, artist, artist_mbid, source,
                    source_recording_mbid, notes, added_at, fulfilled_at)
                   VALUES (:m, :t, :a, :am, :s, :sr, :n, :now, :f)"""
            ),
            {
                "m": payload.rg_mbid,
                "t": payload.title.strip(),
                "a": payload.artist.strip(),
                "am": payload.artist_mbid.strip(),
                "s": payload.source.strip() or "manual",
                "sr": payload.source_recording_mbid.strip(),
                "n": payload.notes.strip(),
                "now": now,
                "f": fulfilled,
            },
        )
    return {
        "ok": True,
        "already_existed": False,
        "rg_mbid": payload.rg_mbid,
        "fulfilled_immediately": fulfilled is not None,
    }


@router.delete("/wishlist/{rg_mbid}")
async def delete_wishlist(rg_mbid: str) -> dict:
    with get_engine().begin() as conn:
        n = conn.execute(
            text("DELETE FROM wishlist WHERE rg_mbid=:m"),
            {"m": rg_mbid},
        ).rowcount
    if n == 0:
        raise HTTPException(404, detail="not in wishlist")
    return {"ok": True, "deleted": rg_mbid}


def _fulfill_matching_wishlist() -> int:
    """扫描完后调一下：把 items 已经命中的 wishlist 项标 fulfilled。
    返回这次新 fulfill 了几条。给 scan worker 用，不暴露 HTTP。"""
    now = int(time.time())
    with get_engine().begin() as conn:
        rows = conn.execute(
            text(
                """SELECT w.rg_mbid FROM wishlist w
                   WHERE w.fulfilled_at IS NULL
                     AND EXISTS (
                         SELECT 1 FROM beets.items i
                         WHERE i.mb_releasegroupid = w.rg_mbid LIMIT 1
                     )"""
            )
        ).all()
        if not rows:
            return 0
        mbids = [r.rg_mbid for r in rows]
        conn.execute(
            text(
                """UPDATE wishlist SET fulfilled_at=:now
                   WHERE rg_mbid IN ({})""".format(
                    ",".join(f":m{i}" for i in range(len(mbids)))
                )
            ),
            {"now": now, **{f"m{i}": m for i, m in enumerate(mbids)}},
        )
    return len(mbids)
