"""task_queue 基础操作."""

from app.workers import queue


def test_enqueue_and_claim(env):
    tid = queue.enqueue("fingerprint", {"item_id": 1})
    assert tid > 0

    task = queue.claim_one()
    assert task is not None
    assert task.kind == "fingerprint"
    assert task.payload == {"item_id": 1}
    assert task.attempts == 1


def test_claim_empty_returns_none(env):
    assert queue.claim_one() is None


def test_enqueue_many(env):
    n = queue.enqueue_many(
        "fingerprint",
        [{"item_id": i} for i in range(5)],
    )
    assert n == 5
    counts = queue.counts_by_status()
    assert counts["queued"] == 5


def test_complete(env):
    tid = queue.enqueue("x", {})
    queue.claim_one()
    queue.complete(tid)
    counts = queue.counts_by_status()
    assert counts["done"] == 1
    assert counts["queued"] == 0


def test_fail_retries_then_dead_letters(env):
    """max_attempts=3 → 前两次 fail 回 queued，第三次进 failed."""
    queue.enqueue("x", {})
    for i in range(1, 4):
        t = queue.claim_one()
        assert t is not None, f"iteration {i} 应该能 claim 到"
        assert t.attempts == i
        queue.fail(t.id, "boom", max_attempts=3)
    # 第 3 次 fail 后 attempts=3, 不再 < 3 → 死信
    assert queue.claim_one() is None
    counts = queue.counts_by_status()
    assert counts["failed"] == 1
    assert counts["queued"] == 0


def test_claim_is_fifo(env):
    queue.enqueue("a", {"i": 1})
    queue.enqueue("a", {"i": 2})
    queue.enqueue("a", {"i": 3})
    seen = [queue.claim_one().payload["i"] for _ in range(3)]
    assert seen == [1, 2, 3]


def test_reset_stuck_running(env):
    import time

    from sqlalchemy import text

    from app.db import get_engine

    queue.enqueue("x", {})
    t = queue.claim_one()
    assert t is not None
    # 倒推 started_at 让它看起来很久没动
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE task_queue SET started_at=:t WHERE id=:id"),
            {"t": int(time.time()) - 9999, "id": t.id},
        )
    n = queue.reset_stuck_running(older_than_sec=300)
    assert n == 1
    assert queue.counts_by_status()["queued"] == 1
