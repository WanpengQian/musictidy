-- 本地策划专辑：MB 没收录的（多见于中文 / 日韩小众 / 厂牌独立 / 演唱会私版）
-- 通过截图 + LLM 解析（或手动输入）落地到本地数据库，让用户能像 MB 真专辑
-- 一样浏览、拖拽绑曲、播放。
--
-- 复用 mb_release_group 表 + 同一份 /playable + drag-bind 链路。只多加：
--   - is_local：标志位，1 = 用户本地策划的
--   - artist_name：没 artist_mbid 时存名字 (MB 真专辑这列保持空)
-- tracks_json 里的 recording_mbid 用我们生成的 uuid4，跟 MB 真 uuid 同格式
-- 但不会撞 MB 命名空间（MB 实际上保留某些子段）；要严格区分以后再加 prefix。

ALTER TABLE mb_release_group ADD COLUMN is_local INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mb_release_group ADD COLUMN artist_name TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_mbrg_is_local
    ON mb_release_group(is_local);
