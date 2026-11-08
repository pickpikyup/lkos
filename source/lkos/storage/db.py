"""SQLite 连接：加载 sqlite-vec、建表、记录与校验 Embedding 元数据。"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import sqlite_vec

from lkos.errors import EmbeddingMismatchError, LkosError, SchemaVersionError

SCHEMA_VERSION = "2"


def connect(db_path: Path | str) -> sqlite3.Connection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # timeout：应用里问答与后台索引各用一个连接，写入冲突时最多等 15 秒
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=15)  # 手动管理事务
    conn.row_factory = sqlite3.Row
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except AttributeError as e:  # macOS 系统自带 Python 不支持加载扩展
        conn.close()
        raise LkosError(
            "当前 Python 的 sqlite3 不支持加载扩展（macOS 自带 Python 常见）。"
            "请用 uv 或 Homebrew 安装的 Python 3.11+ 创建虚拟环境。"
        ) from e
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection, embedding_model: str, embedding_dim: int) -> None:
    """建表（幂等）。首次建表记录 Embedding 元数据；之后校验是否一致。"""
    if int(embedding_dim) % 8:
        raise LkosError("Embedding 维度必须是 8 的倍数（二值向量按字节存储）")
    existing = conn.execute("SELECT name FROM sqlite_master WHERE name = 'index_meta'").fetchone()
    if existing:
        version = get_meta(conn).get("schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"索引格式为 v{version}，当前程序需要 v{SCHEMA_VERSION}（向量存储已改为二值 + int8）。"
                "请删除数据目录下的 lkos.db 后重新索引。"
            )
    sql = resources.files("lkos.storage").joinpath("schema.sql").read_text("utf-8")
    conn.executescript(sql.replace("{dim}", str(int(embedding_dim))))
    _add_missing_columns(conn)
    meta = get_meta(conn)
    if "embedding_model" not in meta:
        set_meta(conn, "schema_version", SCHEMA_VERSION)
        set_meta(conn, "embedding_model", embedding_model)
        set_meta(conn, "embedding_dim", str(embedding_dim))
        return
    check_embedding(meta, embedding_model, embedding_dim)


# 在 v2 格式内新增的列：旧数据库打开时自动补上（只加列，不改已有数据，不需要重建索引）
ADDED_COLUMNS = {
    "sources": [
        ("refresh", "TEXT NOT NULL DEFAULT 'weekly'"),       # manual / daily / weekly / monthly
        ("exclude_json", "TEXT NOT NULL DEFAULT '{}'"),      # {"dirs": [...], "exts": [...]}
        ("ocr", "INTEGER NOT NULL DEFAULT 0"),
        ("allow_online", "INTEGER NOT NULL DEFAULT 1"),       # 是否允许把这个文件夹的内容发往在线模型
        ("ident", "TEXT"),                                    # 文件夹身份（磁盘:inode），改名 / 移动后用来认出它
    ],
    "files": [
        ("tagged_at", "TEXT"),                                # 自动生成标签的时间；内容变化后清空，重新生成
    ],
    "qa_history": [
        ("thread_id", "INTEGER"),                             # 所属对话（第一轮的 id）；旧记录为空，各自算一个对话
        ("branch_of", "INTEGER"),                             # 分支：从哪一轮（原对话里那一轮的 id）分出来

        ("kind", "TEXT NOT NULL DEFAULT 'qa'"),              # qa / summary / extract / analysis
        ("extra_json", "TEXT NOT NULL DEFAULT '{}'"),        # 提取的表格、深度分析的子问题等
    ],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def check_embedding(meta: dict[str, str], embedding_model: str, embedding_dim: int) -> None:
    if meta.get("embedding_model") != embedding_model or meta.get("embedding_dim") != str(embedding_dim):
        raise EmbeddingMismatchError(
            f"索引使用的 Embedding 是 {meta.get('embedding_model')}（{meta.get('embedding_dim')} 维），"
            f"当前配置是 {embedding_model}（{embedding_dim} 维）。"
            "请改回原配置，或删除数据目录下的 lkos.db 后重新索引。"
        )


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM index_meta")}


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO index_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
