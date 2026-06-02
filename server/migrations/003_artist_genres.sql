-- 给 mb_artist 加 genres 字段（JSON 数组：[{"name": "rock", "count": 12}, ...]）
-- 从 MB API get_artist_by_id?inc=genres+tags 拿到，用来给 iOS 端的"按风格搜图"提供素材
ALTER TABLE mb_artist ADD COLUMN genres TEXT;
