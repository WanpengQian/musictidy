"""Playlist endpoints —— 用户从队列里挑出来的曲目组，落 server SQLite。

设计：
- playlist 表存名字 + 时间
- playlist_item 表存 (playlist_id, position) 主键，按 position 排序输出
- 同一 item_id 可在一张 playlist 里出现多次（不去重），用户怎么挑的就怎么存
- title/artist/album_title/rg_mbid 是写入时刻的快照，beets 重 scan 把 item 干掉
  也还能在 UI 上显示"找不到本地文件"
"""

from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import get_engine

router = APIRouter()


class PlaylistItemIn(BaseModel):
    id: int
    title: str = ""
    artist: str = ""
    albumTitle: str = ""
    rgMBID: str = ""


class PlaylistCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    items: list[PlaylistItemIn]


def _gen_id() -> str:
    """跟 web 端 playlist.ts 不冲突的 server id；前缀 sp_ 区分。"""
    return "sp_" + secrets.token_urlsafe(9)


def _row_to_playlist(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "created_at": int(row.created_at),
        "updated_at": int(row.updated_at),
    }


@router.get("/playlists")
async def list_playlists() -> dict:
    """列出所有 playlist + 每个的曲目数。详情走 /playlists/{id}."""
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """SELECT p.id, p.name, p.created_at, p.updated_at,
                          COUNT(pi.position) AS item_count
                   FROM playlist p
                   LEFT JOIN playlist_item pi ON pi.playlist_id = p.id
                   GROUP BY p.id
                   ORDER BY p.updated_at DESC"""
            )
        ).all()
    return {
        "playlists": [
            {**_row_to_playlist(r), "item_count": int(r.item_count or 0)}
            for r in rows
        ]
    }


@router.post("/playlists")
async def create_playlist(payload: PlaylistCreate) -> dict:
    pid = _gen_id()
    now = int(time.time())
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO playlist(id, name, created_at, updated_at)
                   VALUES(:id, :name, :now, :now)"""
            ),
            {"id": pid, "name": payload.name.strip(), "now": now},
        )
        for pos, it in enumerate(payload.items):
            conn.execute(
                text(
                    """INSERT INTO playlist_item
                       (playlist_id, position, item_id, title, artist, album_title, rg_mbid)
                       VALUES(:pid, :pos, :iid, :t, :a, :al, :rg)"""
                ),
                {
                    "pid": pid,
                    "pos": pos,
                    "iid": int(it.id),
                    "t": it.title,
                    "a": it.artist,
                    "al": it.albumTitle,
                    "rg": it.rgMBID,
                },
            )
    return {
        "id": pid,
        "name": payload.name.strip(),
        "created_at": now,
        "updated_at": now,
        "item_count": len(payload.items),
    }


@router.get("/playlists/{pid}")
async def get_playlist(pid: str) -> dict:
    with get_engine().connect() as conn:
        head = conn.execute(
            text(
                """SELECT id, name, created_at, updated_at
                   FROM playlist WHERE id=:id"""
            ),
            {"id": pid},
        ).first()
        if not head:
            raise HTTPException(404, detail="playlist not found")
        items = conn.execute(
            text(
                """SELECT position, item_id, title, artist, album_title, rg_mbid
                   FROM playlist_item WHERE playlist_id=:id
                   ORDER BY position ASC"""
            ),
            {"id": pid},
        ).all()
    return {
        **_row_to_playlist(head),
        "items": [
            {
                "position": int(r.position),
                "id": int(r.item_id),
                "title": r.title or "",
                "artist": r.artist or "",
                "albumTitle": r.album_title or "",
                "rgMBID": r.rg_mbid or "",
            }
            for r in items
        ],
    }


@router.delete("/playlists/{pid}")
async def delete_playlist(pid: str) -> dict:
    with get_engine().begin() as conn:
        # ON DELETE CASCADE 没在 SQLite 默认开，显式删两表
        n1 = conn.execute(
            text("DELETE FROM playlist_item WHERE playlist_id=:id"), {"id": pid}
        ).rowcount
        n2 = conn.execute(
            text("DELETE FROM playlist WHERE id=:id"), {"id": pid}
        ).rowcount
    if n2 == 0:
        raise HTTPException(404, detail="playlist not found")
    return {"deleted": True, "items_removed": int(n1 or 0)}
