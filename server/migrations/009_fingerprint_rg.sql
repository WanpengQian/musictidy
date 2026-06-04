-- track_fingerprint 增加 release_group_mbid 列。
-- 之前只存 recording_mbid, 命中缓存后还得跑一次 MB API 才能拿 rg, 多余;
-- 现在保存时一起带上, 缓存命中直接写回 item.mb_releasegroupid。
ALTER TABLE track_fingerprint ADD COLUMN release_group_mbid TEXT;
CREATE INDEX IF NOT EXISTS idx_fp_rg ON track_fingerprint(release_group_mbid);
