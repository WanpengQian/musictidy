-- AcoustID 一个 recording 通常挂 3-10 个 release-group (原专辑 + 单曲 +
-- N 个 best-of 合集 + 流媒体 EP 等). 之前我们只挑一个存 → 挑错就成 best-of.
-- 现在把全部候选都存下来, 让 dominant-per-folder 后处理跨 dir 多 item
-- 求交集投票 → 在大家都有的那个 rg 里挑非 Compilation 的, 就是原专辑。
ALTER TABLE track_fingerprint ADD COLUMN candidate_rgs TEXT;  -- JSON 数组 of rg_mbid strings
