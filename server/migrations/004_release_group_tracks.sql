-- mb_release_group 加 tracks_json 列，存 MB 上这张专辑的曲目表。
-- 历史上是由 /release-groups/{mbid}/tracks endpoint 第一次跑时懒加，
-- /playable endpoint 直接 SELECT 它，新装的 server / 老 DB 都需要这列存在。
ALTER TABLE mb_release_group ADD COLUMN tracks_json TEXT;
