# ADR 003: 删除 = 软删到 .trash/，30 天后才真清

## 背景

iOS 决策流的核心动作之一是「这首歌不要了，删掉」。但：
- 用户多年攒下的文件，手滑成本极高
- AcoustID 偶尔误判（同名翻唱、live 版本被识别成原曲）
- 用户可能在通勤路上心情不好，30 秒后悔了

## 决策

1. 删除 = `mv` 到 `${DATA_DIR}/trash/`，**永远不 `rm`**
2. trash 文件保留 30 天，每天 0:00 GC 一次过期的
3. trash 命名：`{original_path_hash}_{timestamp}_{original_filename}`，保留 audit 信息
4. 决策 API 加 5 秒 undo 窗：
   - `POST /decide/{id}` 立即返回 `undo_token`，**但不立刻 mv**
   - 5 秒内 `POST /undo/{token}` 可撤
   - 5 秒后才真 mv 到 trash
5. Trash 内容也算应用状态，备份时一并带走

## 命令对照

| 动作 | 实际效果 |
|---|---|
| `decide: trash` | 5 秒后 mv 到 `.trash/`；beets DB 中删条目；写 audit log |
| Trash GC（30 天后） | `rm` 真删 |
| 手动恢复（admin） | 从 `.trash/` mv 回原 path；重新 import |

## 实现要点

- mv 在同一文件系统内是原子操作（FreeBSD/Linux 都保证）；如果 trash 跨文件系统（不推荐）会变成 cp+rm，慢且非原子
- **trash 目录必须和 MUSIC_ROOT 同一个文件系统**，否则 mv 退化
- 因此 `DATA_DIR` 推荐和音乐目录在同一个 zpool / 分区
- GC 用 APScheduler 每天跑

## 与 ALLOW_FILE_WRITES 的关系

- `ALLOW_FILE_WRITES=false`：连软删都不真执行，只在 DB 标记 `decision='trashed'`，文件原地不动。第一版默认这样。
- `ALLOW_FILE_WRITES=true`：才真 mv 到 trash。

这样用户能先跑一段时间，看决策准不准，准了再放开。
