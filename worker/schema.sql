-- LINE ↔ AI Agent 桥接 · D1 表结构
-- 用法：wrangler d1 execute line-agent-bridge --remote --file=./schema.sql

-- 消息队列
CREATE TABLE IF NOT EXISTS inbox (
  id         TEXT PRIMARY KEY,          -- 消息 id
  user_id    TEXT NOT NULL,             -- LINE userId
  text       TEXT,                      -- 文本内容（语音消息为空）
  kind       TEXT DEFAULT 'text',       -- text | audio
  message_id TEXT,                      -- LINE 消息 id（语音取音频用）
  ts         INTEGER NOT NULL,          -- 入队时间（毫秒）
  state      TEXT DEFAULT 'pending',    -- pending | taken
  taken_at   INTEGER                    -- 被本机取走的时间
);

CREATE INDEX IF NOT EXISTS idx_inbox_state ON inbox (state, ts);
CREATE INDEX IF NOT EXISTS idx_inbox_ts    ON inbox (ts);

-- 杂项键值（本机取信时间、owner 等）
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

-- 对话历史（给云端 AI 兜底做上下文）
CREATE TABLE IF NOT EXISTS history (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  role    TEXT NOT NULL,                -- user | assistant
  text    TEXT NOT NULL,
  ts      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_user ON history (user_id, id);
