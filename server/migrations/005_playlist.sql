-- playlist：用户挑出来的一组曲目，跨 session 留存。
-- 当前队列是 client 本地（localStorage），用户主动"另存为播放列表"才落到这里。
--
-- playlist_item.item_id 是 beets items.id —— 如果用户重 scan 把那条记录删了，
-- 这里会变成野指针，所以同时也存 title/artist/album_title 快照用于显示，
-- 播放时按 item_id 命中本地文件，找不到就在 UI 上灰一下。

CREATE TABLE IF NOT EXISTS playlist (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_item (
    playlist_id TEXT NOT NULL,
    position    INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    artist      TEXT NOT NULL DEFAULT '',
    album_title TEXT NOT NULL DEFAULT '',
    rg_mbid     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (playlist_id, position),
    FOREIGN KEY (playlist_id) REFERENCES playlist(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_playlist_item_playlist
    ON playlist_item(playlist_id);
