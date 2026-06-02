# 架构

## 系统图

```
┌──────────────────────────────┐         ┌─────────────────────────────┐
│         iOS (P2)             │         │       Web (P1)              │
│  上滑保留 / 下滑删除 / 左滑   │         │  艺人完整度 / 时间轴 / 搜索  │
│  流式播放 (AAC/FLAC)         │         │  htmx + Jinja               │
└──────────────┬───────────────┘         └──────────────┬──────────────┘
               │ HTTPS                                   │ HTTPS
               └───────────────┬─────────────────────────┘
                               │
                        ┌──────▼──────┐
                        │   Caddy     │  自动 TLS + 反代
                        └──────┬──────┘
                               │
                        ┌──────▼──────────────────────────┐
                        │  FastAPI app (uvicorn)          │
                        │                                 │
                        │  ┌───────────────────────────┐  │
                        │  │ /api/v1/artists ...       │  │
                        │  │ /api/v1/curation/...      │  │
                        │  │ /  (HTML routes, htmx)    │  │
                        │  └───────────────────────────┘  │
                        │                                 │
                        │  ┌──── Background tasks ─────┐  │
                        │  │ APScheduler:              │  │
                        │  │  - scan (6h)              │  │
                        │  │  - mb refresh (weekly)    │  │
                        │  │  - trash gc (daily)       │  │
                        │  │ asyncio worker drain      │  │
                        │  │ task_queue (SQLite)       │  │
                        │  └───────────────────────────┘  │
                        │                                 │
                        │  ┌── Subprocess pool ─────────┐ │
                        │  │ ffmpeg (transcode)         │ │
                        │  │ fpcalc (fingerprint)       │ │
                        │  └────────────────────────────┘ │
                        └────────┬────────────────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
   ┌────▼─────┐            ┌────▼──────┐            ┌────▼────┐
   │ library  │            │ musictidy │            │  音乐    │
   │  .db     │            │   .db     │            │  files  │
   │ (beets)  │            │ (我们的)   │            │  (RO)   │
   └──────────┘            └───────────┘            └─────────┘
                                 │
                          ┌──────▼──────┐
                          │ MusicBrainz │
                          │ (1 req/sec) │
                          └─────────────┘
```

## 责任分层

| 层 | 职责 | 跨进程？ |
|---|---|---|
| Caddy | TLS、反代、access log | 独立进程 |
| FastAPI / uvicorn | HTTP API、HTML 渲染、调度 | 单进程，async |
| beets (作为 Python 库) | 库扫描、autotagger、move 模板 | 同进程 |
| ffmpeg/fpcalc | 转码、指纹 | subprocess pool |
| SQLite (library.db) | beets 拥有 | 文件 |
| SQLite (musictidy.db) | 我们的扩展（MB cache / 决策 / 队列） | 文件 |

**关键决策**：所有应用逻辑跑在一个 uvicorn 进程里。后台任务用 asyncio 协程 + APScheduler 触发，不引 Celery/Redis/RQ。SQLite 同时承担「数据库」和「轻量任务队列」两个角色。理由见 [`decisions/002-no-redis-celery.md`](decisions/002-no-redis-celery.md)。

## 数据流：扫库 → 识别 → 完整度

```
1. APScheduler 触发 scan_and_import (每 6h)
   ├─ walk MUSIC_ROOT 找音频文件
   ├─ diff vs beets 已知 paths
   └─ 新文件 → beets.import (autotag=True, copy=False, move=False)
                            ↑
                            └─ 拿到 mb_artistid / mb_albumid / mb_releasegroupid

2. 扫完后，对所有未指纹化的 item:
   └─ enqueue("fingerprint", {item_id})

3. asyncio worker 慢慢消费 task_queue:
   ├─ fingerprint: fpcalc + acoustid lookup
   ├─ mb_fetch_artist: musicbrainzngs.get_artist (1 req/sec lock)
   ├─ mb_fetch_rg_list: 拿一个艺人的全部 release-group
   └─ cover_fetch: coverartarchive.org/release-group/{mbid}/front

4. 网页 /artists 查询:
   └─ JOIN beets.items × mb_release_group × mb_artist
        → 现成的 completeness 数字
```

## 数据流：iOS 决策（P2）

```
GET /api/v1/curation/next
   └─ SELECT item FROM beets.items
      LEFT JOIN item_decision ON ...
      WHERE decision.state IS NULL OR decision.state = 'review_later'
      ORDER BY RANDOM() LIMIT 1
      → 拼上 current_tags / suggested_tags / dupes
      → 返回 stream_url

GET /api/v1/curation/stream/{id}?codec=aac
   ├─ cache hit? → FileResponse (支持 Range, AVPlayer seek OK)
   └─ cache miss? → ffmpeg pipe → StreamingResponse + 同步落盘

POST /api/v1/curation/decide/{id}
   ├─ keep: UPDATE item_decision SET state='kept'
   ├─ trash: mv 文件到 trash/ + beets remove + 决策 log
   ├─ auto_organize: beets.move_item (按 path template)
   ├─ retag: mutagen.write + beets.refresh
   └─ resolve_dup: trash 输家 + keep 赢家
   → 返回 undo_token，5 秒内可撤
```

## 转码细节

**为什么必须服务器转码？**

- iOS AVFoundation **不支持 APE** (Monkey's Audio)。FLAC 11+ 原生支持，MP3/AAC/ALAC 都支持。
- 用户库里大量 APE → 必须服务器侧解码再喂客户端。

**策略**：

| 网络 | 源格式 | 客户端拿到 |
|---|---|---|
| WiFi | FLAC | FLAC 直传（pass-through） |
| WiFi | APE / ALAC | 转 FLAC |
| WiFi | MP3 / AAC | 直传 |
| Cellular | 任意 | 转 AAC 256kbps |

**实现**：subprocess + pipe。ffmpeg stdout → StreamingResponse。同时落盘到 LRU `transcode_cache/`。命中 cache 直接 FileResponse（FileResponse 天然支持 Range，AVPlayer seek 工作正常）。

**Content-Length**：第一次流式 response 不知道精确字节数。用 `源时长 × 目标 bitrate / 8` 估算作为 Content-Length，AVPlayer 进度条能用。误差 <5% 用户无感。

**并发**：同一文件并发请求 → 用 `dict[cache_key, asyncio.Event]` 去重，第二个请求 await Event。

**Seek 在 cache 完成前**：第一版直接禁——detect Range header 且 cache 未就绪 → 返回 503 + Retry-After，客户端等 1s 重试。简单粗暴但用户基本无感（绝大多数情况开播后几秒 cache 就好了）。

## 完整度查询

```sql
ATTACH DATABASE 'library.db' AS beets;

WITH owned_rg AS (
    SELECT DISTINCT mb_releasegroupid AS mbid
    FROM beets.items
    WHERE mb_releasegroupid != ''
)
SELECT
    a.mbid, a.name, a.sort_name,
    COUNT(DISTINCT rg.mbid) AS total,
    COUNT(DISTINCT o.mbid) AS owned,
    CAST(COUNT(DISTINCT o.mbid) AS REAL)
        / NULLIF(COUNT(DISTINCT rg.mbid), 0) AS completeness
FROM mb_artist a
JOIN mb_release_group rg ON rg.artist_mbid = a.mbid
LEFT JOIN owned_rg o ON o.mbid = rg.mbid
WHERE rg.primary_type IN ('Album', 'EP')
  AND (rg.secondary_types IS NULL OR rg.secondary_types = '[]')
GROUP BY a.mbid
HAVING total > 0
ORDER BY completeness DESC NULLS LAST, owned DESC;
```

**Track 级 partial 判断**（artist 详情页）：beets 已经记了 `mb_albumid`（release MBID）。查那个 release 的 tracklist（musicbrainzngs `get_release_by_id` with `recordings` include）→ 比对 `beets.items WHERE mb_albumid = ?` 的 count → < 全部就标 partial。

## 并发与锁

| 资源 | 并发策略 |
|---|---|
| `beets.lib`（SQLAlchemy session） | 一个写锁；读用单独 connection |
| MusicBrainz API | `asyncio.Semaphore(1)` + 1.05s 间隔，全局 |
| ffmpeg 进程 | `asyncio.Semaphore(N)`，N 默认 2，FreeBSD NAS 上配 1 |
| 同一文件转码 | per-key Event 去重 |
| `task_queue` 取活 | `UPDATE ... WHERE id = (SELECT ... LIMIT 1)` + retry on conflict |

## 配置传递链

```
.env (env_file)
  └─→ Pydantic Settings (app/config.py)
        └─→ FastAPI dependency injection
```

不读 yaml 之外的配置文件（beets 自己的 config.yaml 例外，那是 beets 的事）。

## 错误处理原则

- 上游 API（MusicBrainz、Cover Art）失败不阻塞主流程，记 last_error 进 task_queue，下次重试。
- ffmpeg 失败 → 给客户端 502 + 删除半截 cache 文件。
- beets 导入失败（identify 不出来）→ 文件还在原地，标记 `mb_*` 为 NULL，进 review-later 队列。
- 任何「写音乐文件」的操作（retag / move）在 `ALLOW_FILE_WRITES=false` 时直接拒绝，开发期完全只读。

## 监控

最小：`/api/v1/admin/stats` 给个 JSON：

- 总文件数 / 已 import / 已指纹 / 已识别（有 MBID）
- 任务队列各 status 计数
- 转码缓存大小
- 最近 7 天 decision 计数（kept / trashed）

后续可挂 Prometheus，但单用户场景一个 stats endpoint 够了。
