# ADR 002: 不引 Redis / Celery，用 SQLite 当任务队列

## 背景

需要后台跑：
- 文件系统扫描（周期）
- 指纹 + AcoustID 匹配（受 fpcalc 速度限）
- MusicBrainz 调用（1 req/sec）
- 封面下载
- trash GC（每天）

## 决策

- **不引 Redis 或 Celery**。
- 用 SQLite 一张 `task_queue` 表 + 应用内 asyncio worker。
- 周期触发用 APScheduler，挂在同一个 FastAPI 进程里。

## 队列协议（极简）

```sql
CREATE TABLE task_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER
);
```

取活：

```sql
UPDATE task_queue
SET status='running', started_at=:now
WHERE id = (
    SELECT id FROM task_queue
    WHERE status='queued'
    ORDER BY created_at LIMIT 1
)
RETURNING *;
```

完成：

```sql
UPDATE task_queue SET status='done', finished_at=:now WHERE id=:id;
```

## 为什么这够用

- 单用户、单 host，从来不存在 10 个 worker 抢一个任务的局面。
- SQLite WAL 模式下，UPDATE...RETURNING 原子，并发安全。
- 队列规模：扫一次库 + 给每个文件指纹 + 给每个艺人拉 MB ≈ 10⁵ 量级，SQLite 处理这个完全没压力。
- 失败重试用 `attempts` 字段控制；超过阈值 → `status='failed'`，admin 页能看到。

## 什么时候推翻这个决策

- 跑到多 host 部署（不可能，单用户场景）
- 任务上百万规模（也不太可能，库 TB 级也就 10 万 song）
- 需要外部触发（webhooks）大量进来

那之前，这条决策稳定。
