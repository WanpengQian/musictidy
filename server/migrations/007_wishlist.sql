-- 心愿单：用户在外面 Shazam 听到一首歌后，把整张专辑标记成"想要"。
-- 后面用户慢慢去找 / 扒 / 买；当 scan worker 把文件搞进库、items 拿到对应
-- mb_releasegroupid 后，fulfilled_at 自动填上，UI 上变绿打勾。
--
-- 数据来源：
--   - source='shazam'：iOS Shazam 卡上点的"加心愿单"
--   - source='manual'：用户在专辑详情里手动加
--   - source='share' 等：以后扩展，朋友分享过来的等
--
-- 不存图片：cover 走 /api/v1/covers/release-group/{mbid}/500（CAA 真专辑）。
-- 同一张专辑加多次不复制 → rg_mbid 主键。

CREATE TABLE IF NOT EXISTS wishlist (
    rg_mbid               TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    artist                TEXT NOT NULL DEFAULT '',
    artist_mbid           TEXT NOT NULL DEFAULT '',
    source                TEXT NOT NULL DEFAULT 'manual',
    source_recording_mbid TEXT NOT NULL DEFAULT '',
    notes                 TEXT NOT NULL DEFAULT '',
    added_at              INTEGER NOT NULL,
    fulfilled_at          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_wishlist_added_at
    ON wishlist(added_at DESC);
CREATE INDEX IF NOT EXISTS idx_wishlist_fulfilled
    ON wishlist(fulfilled_at);
