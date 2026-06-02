"""周期任务调度 + 持续 worker drain loop.

两个机制：
1. APScheduler：周期性触发（scan / mb_refresh / trash_gc）
2. asyncio.Task：持续从 task_queue 取活，dispatch 给对应 handler

为什么分开：周期触发用 APScheduler 表达清晰；queue drain 是 forever-loop
配 backoff sleep，APScheduler 的 interval 会因为上一轮还没结束而堆 job。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path as PPath

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.workers import queue

log = logging.getLogger(__name__)


# ── handlers 注册表 ─────────────────────────────────────────────
# kind → async handler(payload) -> None
Handler = Callable[[dict], Awaitable[None]]
_handlers: dict[str, Handler] = {}


def register_handler(kind: str, handler: Handler) -> None:
    _handlers[kind] = handler
    log.info("queue: registered handler for kind=%s", kind)


# ── drain workers ───────────────────────────────────────────────
# worker 数 / 各 kind 并发上限都从 settings 读，方便低配机调小
_drain_tasks: list[asyncio.Task] = []
_stop_event: asyncio.Event | None = None
_busy_dirs: set[str] = set()
_kind_sems: dict[str, asyncio.Semaphore] = {}


def _get_kind_sem(kind: str) -> asyncio.Semaphore | None:
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    if kind == "fingerprint":
        return _kind_sems.setdefault(
            "fingerprint",
            asyncio.Semaphore(s.queue_fingerprint_concurrency),
        )
    if kind == "mb_fetch_artist":
        return _kind_sems.setdefault(
            "mb_fetch_artist",
            asyncio.Semaphore(s.queue_mb_artist_concurrency),
        )
    return None


def _dir_key_for(kind: str, payload: dict) -> str | None:
    """从 payload 抽出"目录键"用于互斥。同目录任务串行，跨目录并行。

    archive_extract: 用 archive 文件全路径而非父目录 —— 每个 archive 解到
        `_extracted/<archive_stem>/` 各自独立，不冲突；只防止同一档案被两个
        worker 抢着解。
    cue_split: 用 cue 文件全路径 —— 同理。
    """
    if kind == "archive_extract":
        return PPath(payload["archive"]).as_posix()
    if kind == "cue_split":
        return PPath(payload["cue"]).as_posix()
    return None


async def _drain_worker(worker_id: int) -> None:
    """单个 worker：claim → 检查目录冲突 → run / requeue。"""
    assert _stop_event is not None
    backoff = 1.0
    log.info("queue: drain worker %d started", worker_id)
    while not _stop_event.is_set():
        task = queue.claim_one()
        if task is None:
            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 1.5, 10.0)
            continue
        backoff = 1.0

        dir_key = _dir_key_for(task.kind, task.payload)
        if dir_key and dir_key in _busy_dirs:
            # 这个目录已有 worker 在跑，让回去；小睡免空转
            queue.requeue(task.id)
            await asyncio.sleep(0.2)
            continue

        handler = _handlers.get(task.kind)
        if handler is None:
            queue.fail(task.id, f"no handler registered for kind={task.kind}")
            log.warning("queue: dropped task %d kind=%s (no handler)", task.id, task.kind)
            continue

        if dir_key:
            _busy_dirs.add(dir_key)
        kind_sem = _get_kind_sem(task.kind)
        try:
            if kind_sem is not None:
                async with kind_sem:
                    await handler(task.payload)
            else:
                await handler(task.payload)
            queue.complete(task.id)
        except queue.PermanentTaskError as e:
            # handler 明确说"别重试" → 一次性 failed
            log.warning("queue: task %d kind=%s permanent failure: %s",
                        task.id, task.kind, e)
            queue.fail(task.id, str(e), max_attempts=0)
        except Exception as e:  # noqa: BLE001
            log.exception("queue: task %d kind=%s failed", task.id, task.kind)
            queue.fail(task.id, repr(e))
        finally:
            if dir_key:
                _busy_dirs.discard(dir_key)

    log.info("queue: drain worker %d stopped", worker_id)


# ── APScheduler ─────────────────────────────────────────────────
_sched: AsyncIOScheduler | None = None


def _wrap_async(coro_factory):
    """APScheduler 接受 callable；包一层把 coroutine 投到 loop."""

    def _run():
        asyncio.create_task(coro_factory())

    return _run


def start() -> None:
    """在 FastAPI lifespan 里调用。"""
    global _sched, _stop_event

    if _sched is not None:
        log.warning("scheduler: already started")
        return

    # 启动时回收一波 crash 留下的 running 任务
    recovered = queue.reset_stuck_running()
    if recovered:
        log.info("queue: recovered %d stuck running tasks", recovered)

    from app.config import get_settings  # noqa: PLC0415

    n_workers = get_settings().queue_workers
    log.info("scheduler: starting %d drain workers", n_workers)
    _stop_event = asyncio.Event()
    for i in range(n_workers):
        t = asyncio.create_task(_drain_worker(i), name=f"queue-drain-{i}")
        _drain_tasks.append(t)

    _sched = AsyncIOScheduler()

    # 周期任务（handler 实际函数延迟 import，避免循环依赖）
    from app.workers.scan import scan_and_import  # noqa: PLC0415

    _sched.add_job(
        _wrap_async(scan_and_import),
        trigger="interval",
        minutes=30,
        id="scan",
        # 启动 30 秒后跑首次，之后每 30 min 一轮
        next_run_time=datetime.now() + timedelta(seconds=30),
    )

    # TODO: trash_gc, mb_refresh —— 等对应 worker 实现后再注册

    _sched.start()
    log.info("scheduler: started (jobs=%s)", [j.id for j in _sched.get_jobs()])


async def stop() -> None:
    """在 FastAPI lifespan 退出时调用."""
    global _sched, _stop_event

    if _stop_event is not None:
        _stop_event.set()
    if _drain_tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*_drain_tasks, return_exceptions=True),
                timeout=5.0,
            )
        except TimeoutError:
            log.warning("drain workers did not stop in 5s; cancelling")
            for t in _drain_tasks:
                t.cancel()
        _drain_tasks.clear()
    if _sched is not None:
        _sched.shutdown(wait=False)
        _sched = None
    _stop_event = None
    log.info("scheduler: stopped")
