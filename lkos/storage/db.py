"""SQLite 连接：加载 sqlite-vec、建表、记录与校验 Embedding 元数据。"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import sqlite_vec

from lkos.errors import EmbeddingMismatchError, LkosError

SCHEMA_VERSION = "1"


def connect(db_path: Path | str) -> sqlite3.Connection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # 手动管理事务
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
    sql = resources.files("lkos.storage").joinpath("schema.sql").read_text("utf-8")
    conn.executescript(sql.replace("{dim}", str(int(embedding_dim))))
    meta = get_meta(conn)
    if "embedding_model" not in meta:
        set_meta(conn, "schema_version", SCHEMA_VERSION)
        set_meta(conn, "embedding_model", embedding_model)
        set_meta(conn, "embedding_dim", str(embedding_dim))
        return
    check_embedding(meta, embedding_model, embedding_dim)


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
