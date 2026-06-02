-- 登录 session token 表

CREATE TABLE IF NOT EXISTS auth_session (
    token         TEXT PRIMARY KEY,
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL,
    user_agent    TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_exp ON auth_session(expires_at)
