-- Local Knowledge OS schema v2
-- v2 相对设计文档第 6 节的变化（面向 100 GB 级语料）：
--   向量改为二值粗筛（chunks_vec，bit）+ int8 精排（vectors），不再存 float32；
--   vectors 按文本哈希去重，作为 Embedding 缓存；index_runs 记录复用数与耗时。
-- {dim} 在建表时替换为配置中的 Embedding 维度。

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    owner_id    TEXT NOT NULL DEFAULT 'local',
    path        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    owner_id      TEXT NOT NULL DEFAULT 'local',
    path          TEXT NOT NULL UNIQUE,
    file_type     TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('indexed', 'failed', 'skipped')),
    error         TEXT,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    indexed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_source ON files(source_id);

CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    owner_id      TEXT NOT NULL DEFAULT 'local',
    ordinal       INTEGER NOT NULL,
    text          TEXT NOT NULL,
    lang          TEXT NOT NULL,
    page          INTEGER,
    heading_path  TEXT,
    para_start    INTEGER,
    para_end      INTEGER,
    line_start    INTEGER,
    line_end      INTEGER,
    source_kind   TEXT NOT NULL DEFAULT 'text',
    text_hash     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(text_hash);

-- 向量缓存：同一文本只存一份。emb = int8（每向量单独缩放），bits = 二值向量
CREATE TABLE IF NOT EXISTS vectors (
    hash  TEXT PRIMARY KEY,
    emb   BLOB NOT NULL,
    bits  BLOB NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    tokens,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- 二值向量粗筛表：每块 dim/8 字节，lang 为元数据列，严格语言模式下可直接过滤
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    chunk_id   INTEGER PRIMARY KEY,
    lang       TEXT,
    embedding  BIT[{dim}]
);

CREATE TABLE IF NOT EXISTS index_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_runs (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    aborted        INTEGER NOT NULL DEFAULT 0,
    files_seen     INTEGER DEFAULT 0,
    files_indexed  INTEGER DEFAULT 0,
    files_skipped  INTEGER DEFAULT 0,
    files_moved    INTEGER DEFAULT 0,
    files_failed   INTEGER DEFAULT 0,
    files_removed  INTEGER DEFAULT 0,
    chunks_added   INTEGER DEFAULT 0,
    chunks_reused  INTEGER DEFAULT 0,
    parse_ms       INTEGER DEFAULT 0,
    embed_ms       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS call_logs (
    id                INTEGER PRIMARY KEY,
    owner_id          TEXT NOT NULL DEFAULT 'local',
    ts                TEXT NOT NULL,
    task              TEXT NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    latency_ms        INTEGER,
    ttft_ms           INTEGER,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    mem_available_mb  INTEGER,
    swap_used_mb      INTEGER,
    error             TEXT
);


-- 问答记录（应用界面的历史列表）；context_json 保存送入模型的片段，原文件变化后仍能回看当时的出处
CREATE TABLE IF NOT EXISTS qa_history (
    id            INTEGER PRIMARY KEY,
    owner_id      TEXT NOT NULL DEFAULT 'local',
    asked_at      TEXT NOT NULL,
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    not_found     INTEGER NOT NULL DEFAULT 0,
    context_json  TEXT NOT NULL DEFAULT '[]',
    cited_json    TEXT NOT NULL DEFAULT '[]',
    model         TEXT,
    ttft_ms       INTEGER,
    latency_ms    INTEGER
);
