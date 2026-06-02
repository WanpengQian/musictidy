"""SQLite-backed task queue.

设计原则在 docs/decisions/002-no-redis-celery.md。

并发安全：UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING 在 WAL 模式
下是原子的。单进程多协程取活不会争同一行。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.db import get_engine

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Task:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int


def enqueue(kind: str, payload: dict[str, Any]) -> int:
    """投递一个任务，返回 task id."""
    with get_engine().begin() as conn:
        result = conn.execute(
            text(
                """INSERT INTO task_queue (kind, payload, status, created_at)
                   VALUES (:kind, :payload, 'queued', :now)
                   RETURNING id"""
            ),
            {"kind": kind, "payload": json.dumps(payload), "now": int(time.time())},
        )
        row = result.first()
        return int(row.id) if row else -1


def enqueue_many(kind: str, payloads: list[dict[str, Any]]) -> int:
    """批量投递；返回投递条数."""
    if not payloads:
        return 0
    now = int(time.time())
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO task_queue (kind, payload, status, created_at)
                   VALUES (:kind, :payload, 'queued', :now)"""
            ),
            [
                {"kind": kind, "payload": json.dumps(p), "now": now}
                for p in payloads
            ],
        )
    return len(payloads)


def claim_one() -> Task | None:
    """原子地抢一个 queued 任务，状态置为 running。无任务返回 None."""
    now = int(time.time())
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                """UPDATE task_queue
                       SET status='running',
                           started_at=:now,
                           attempts=attempts+1
                   WHERE id = (
                       SELECT id FROM task_queue
                       WHERE status='queued'
                       ORDER BY created_at
                       LIMIT 1
                   )
                   RETURNING id, kind, payload, attempts"""
            ),
            {"now": now},
        ).first()
        if not row:
            return None
        return Task(
            id=int(row.id),
            kind=str(row.kind),
            payload=json.loads(row.payload),
            attempts=int(row.attempts),
        )


def complete(task_id: int) -> None:
    """标记任务成功完成."""
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """UPDATE task_queue
                       SET status='done', finished_at=:now, last_error=NULL
                   WHERE id=:id"""
            ),
            {"id": task_id, "now": int(time.time())},
        )


class PermanentTaskError(Exception):
    """Handler 抛这个表示"别重试了，直接失败"——比如源文件根本不是有效的 archive。
    scheduler 会把任务一次性标 'failed'，跳过 5 次重试。"""


def fail(task_id: int, error: str, *, max_attempts: int = 5) -> None:
    """标记失败；attempts < max_attempts 则回到 queued 重试，否则 failed."""
    now = int(time.time())
    truncated_error = error[:1000]  # 别写满
    with get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT attempts FROM task_queue WHERE id=:id"),
            {"id": task_id},
        ).first()
        if not row:
            return
        next_status = "queued" if row.attempts < max_attempts else "failed"
        conn.execute(
            text(
                """UPDATE task_queue
                       SET status=:s, last_error=:err,
                           finished_at=CASE WHEN :s='failed' THEN :now ELSE NULL END
                   WHERE id=:id"""
            ),
            {"id": task_id, "s": next_status, "err": truncated_error, "now": now},
        )


def counts_by_status() -> dict[str, int]:
    out: dict[str, int] = {"queued": 0, "running": 0, "done": 0, "failed": 0}
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT status, COUNT(*) AS c FROM task_queue GROUP BY status")
        ).all()
        for r in rows:
            out[str(r.status)] = int(r.c)
    return out


def counts_by_kind_status() -> list[dict[str, Any]]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """SELECT kind, status, COUNT(*) AS c
                   FROM task_queue
                   GROUP BY kind, status
                   ORDER BY kind, status"""
            )
        ).all()
        return [{"kind": r.kind, "status": r.status, "count": r.c} for r in rows]


def requeue(task_id: int) -> None:
    """把任务塞回 queued —— worker 因为目录被占而让出时用，不算消耗一次重试."""
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """UPDATE task_queue
                       SET status='queued',
                           started_at=NULL,
                           attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END
                   WHERE id=:id"""
            ),
            {"id": task_id},
        )


def reset_stuck_running(older_than_sec: int = 300) -> int:
    """启动时回收：之前 crash 留下的 running 任务超过 5 分钟没 finish 的，
    扔回 queued 让它重试。返回回收条数."""
    cutoff = int(time.time()) - older_than_sec
    with get_engine().begin() as conn:
        result = conn.execute(
            text(
                """UPDATE task_queue
                       SET status='queued', started_at=NULL
                   WHERE status='running' AND started_at < :cutoff"""
            ),
            {"cutoff": cutoff},
        )
        return result.rowcount or 0
