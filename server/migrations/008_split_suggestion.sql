-- 整张专辑做成一个大 FLAC/APE/WAV (没 CUE) 的自动检测建议。
--
-- 来源：fingerprint worker 写完 rg_mbid 后, 如果
--   - item 是 lossless 单文件 (FLAC/WAV/APE), length > 1500s
--   - 它的 rg 在 mb_release_group.tracks_json 里有 ≥3 首
--   - 总 MB 时长 ≈ item 时长 (±60s)
-- 就 INSERT 一行；前端读 /api/v1/split-suggestions 在顶栏弹气泡，
-- 用户一键 apply → 真正调 /items/{id}/split-by-album。
--
-- 不在文件层面做任何破坏；用户点了 apply 才切。dismiss 只是隐藏建议。
-- 同一个 (item_id, rg_mbid) 重复检测幂等。

CREATE TABLE IF NOT EXISTS split_suggestion (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    beets_item_id       INTEGER NOT NULL,
    rg_mbid             TEXT NOT NULL,
    file_length_s       REAL NOT NULL,
    mb_total_length_s   REAL NOT NULL,
    mb_track_count      INTEGER NOT NULL,
    file_path           TEXT NOT NULL DEFAULT '',
    detected_at         INTEGER NOT NULL,
    dismissed_at        INTEGER,
    applied_at          INTEGER,
    UNIQUE(beets_item_id, rg_mbid)
);

CREATE INDEX IF NOT EXISTS idx_split_sugg_pending
    ON split_suggestion(detected_at DESC)
    WHERE dismissed_at IS NULL AND applied_at IS NULL;
