-- Local Knowledge OS POC schema（设计文档第 6 节）
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
    source_kind   TEXT NOT NULL DEFAULT 'text'
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    tokens,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    chunk_id   INTEGER PRIMARY KEY,
    lang       TEXT,
    embedding  FLOAT[{dim}] distance_metric=cosine
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
    chunks_added   INTEGER DEFAULT 0
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
