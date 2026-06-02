"""转码结果 LRU 缓存.

落盘文件 + SQLite 索引 (transcode_cache 表)。
超过 TRANSCODE_CACHE_GB 上限时按 last_used 升序淘汰。
"""

# TODO P2.
