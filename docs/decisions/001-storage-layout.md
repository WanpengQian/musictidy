# ADR 001: 存储布局 —— 两个 SQLite 文件，不动音乐目录

## 背景

用户音乐库 TB 级、多年未整理。需要存放：
- 文件元数据（path、tags、format、MBID 等）
- 我们自己的扩展（MusicBrainz 缓存、决策状态、任务队列、转码缓存索引）
- 转码后的音频文件（LRU 缓存）
- 软删的音乐文件

## 决策

1. **两个 SQLite 文件**：
   - `library.db` — beets 自己拥有，我们只读
   - `musictidy.db` — 我们的扩展，应用层 ATTACH 后 JOIN
2. **音乐目录只读挂载**（Docker） / 只读权限（FreeBSD），直到 `ALLOW_FILE_WRITES=true` 才允许写
3. **trash/、transcode_cache/、covers/** 都在 `DATA_DIR`，跟两个 db 平级
4. 默认 `DATA_DIR=/var/db/musictidy`（FreeBSD 习惯）

## 为什么不合并成一个 db

- beets 升级时它自己会 migrate library.db。如果在它的 db 里加表，升级容易冲突。
- 分开后，beets 升级 = 零风险；我们的 schema 演进也独立。
- ATTACH DATABASE 性能损失可以忽略（同进程同磁盘）。

## 为什么不用 Postgres

- 单用户、单进程，SQLite 完全够。
- 备份 = `cp library.db backup.db`，傻瓜。
- WAL 模式下并发读写也 OK。

## 备份策略

```sh
tar -czf backup.tar.gz $DATA_DIR
```

就这。 trash 也在里面，因此误删 30 天内都能找回。
