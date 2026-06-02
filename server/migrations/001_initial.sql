-- MusicTidy 初始 schema (musictidy.db)
-- 这些表跟 beets 的 library.db 平级；查询时 ATTACH library.db AS beets 再 JOIN。

-- ── MusicBrainz 缓存（dashboard 用）──────────────────────────
CREATE TABLE IF NOT EXISTS mb_artist (
    mbid           TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    sort_name      TEXT,
    country        TEXT,
    disambiguation TEXT,
    fetched_at     INTEGER NOT NULL,
    stale_after    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mb_release_group (
    mbid               TEXT PRIMARY KEY,
    artist_mbid        TEXT NOT NULL,
    title              TEXT NOT NULL,
    primary_type       TEXT,
    secondary_types    TEXT,
    first_release_date TEXT,
    cover_url          TEXT,
    FOREIGN KEY (artist_mbid) REFERENCES mb_artist(mbid)
);

CREATE INDEX IF NOT EXISTS idx_rg_artist ON mb_release_group(artist_mbid);

-- ── 决策状态（iOS 决策端用）──────────────────────────────────
CREATE TABLE IF NOT EXISTS item_decision (
    beets_item_id  INTEGER PRIMARY KEY,
    state          TEXT NOT NULL,
    decided_at     INTEGER,
    decision_note  TEXT
);

CREATE INDEX IF NOT EXISTS idx_dec_state ON item_decision(state);

-- ── 任务队列 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS task_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    attempts     INTEGER DEFAULT 0,
    last_error   TEXT,
    created_at   INTEGER NOT NULL,
    started_at   INTEGER,
    finished_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tq_status ON task_queue(status, created_at);

-- ── 转码缓存索引 ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transcode_cache (
    cache_key   TEXT PRIMARY KEY,
    item_id     INTEGER NOT NULL,
    codec       TEXT NOT NULL,
    bitrate     INTEGER,
    file_path   TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    last_used   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cache_last_used ON transcode_cache(last_used);

-- ── 软删日志（保留 audit 信息）──────────────────────────────
CREATE TABLE IF NOT EXISTS trash_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    original_path   TEXT NOT NULL,
    trash_path      TEXT NOT NULL,
    original_size   INTEGER,
    trashed_at      INTEGER NOT NULL,
    gc_after        INTEGER NOT NULL,
    restored_at     INTEGER,
    gc_done_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_trash_gc ON trash_log(gc_after) WHERE gc_done_at IS NULL;

-- ── 待 apply 的延迟决策（5 秒 undo 窗内）──────────────────────
CREATE TABLE IF NOT EXISTS pending_decision (
    undo_token       TEXT PRIMARY KEY,
    beets_item_id    INTEGER NOT NULL,
    action           TEXT NOT NULL,
    payload          TEXT NOT NULL,
    apply_after      INTEGER NOT NULL,
    cancelled_at     INTEGER,
    applied_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pending_apply ON pending_decision(apply_after)
    WHERE applied_at IS NULL AND cancelled_at IS NULL
