"""所有 SQL 读写集中在这里。

注意：FTS5 与 vec0 虚拟表不会随外键级联删除，删除文本块时必须同时删除它们的行。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Sequence

import sqlite_vec

from lkos.storage.vectors import text_hash, to_int8
from lkos.types import CallLog, Chunk, FileInfo, FileRecord, Location, RetrievedChunk


def now_iso(precise: bool = False) -> str:
    """precise=True 时精确到微秒（files.indexed_at：重新切块续跑时据此判断文件是否已处理）。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds" if precise else "seconds")


@dataclass
class IndexRun:
    id: int
    source_id: int
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_moved: int = 0
    files_failed: int = 0
    files_removed: int = 0
    chunks_added: int = 0
    chunks_reused: int = 0  # 命中 Embedding 缓存、没有重新计算向量的块
    parse_ms: int = 0
    embed_ms: int = 0
    aborted: bool = False


class Repository:
    def __init__(self, conn: sqlite3.Connection, owner_id: str = "local"):
        self.conn = conn
        self.owner_id = owner_id

    # ---------- 事务 ----------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---------- 来源 ----------
    def upsert_source(self, path: str) -> int:
        row = self.conn.execute("SELECT id FROM sources WHERE path = ?", (path,)).fetchone()
        if row:
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO sources(owner_id, path, created_at) VALUES (?, ?, ?)",
            (self.owner_id, path, now_iso()),
        )
        return int(cur.lastrowid)

    def list_sources(self) -> list[dict]:
        """来源文件夹及其文件数、文本块数、最近一次索引时间。"""
        rows = self.conn.execute(
            """SELECT s.id, s.path,
                      (SELECT COUNT(*) FROM files f WHERE f.source_id = s.id) AS files,
                      (SELECT COUNT(*) FROM files f WHERE f.source_id = s.id AND f.status = 'failed') AS failed,
                      (SELECT COUNT(*) FROM chunks c JOIN files f ON f.id = c.file_id WHERE f.source_id = s.id) AS chunks,
                      (SELECT MAX(r.finished_at) FROM index_runs r WHERE r.source_id = s.id AND r.aborted = 0) AS last_indexed
               FROM sources s ORDER BY s.id""").fetchall()
        return [dict(r) for r in rows]

    def remove_source(self, source_id: int) -> int:
        """删除来源文件夹及其全部索引（不动原文件），返回删除的文件数。"""
        ids = [f.id for f in self.files_of_source(source_id)]
        self.remove_files(ids)
        with self.transaction():
            self.conn.execute("DELETE FROM index_runs WHERE source_id = ?", (source_id,))
            self.conn.execute("DELETE FROM index_meta WHERE key IN (?, ?)",
                              (f"chunker:{source_id}", f"rechunk:{source_id}"))
            self.conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        self.prune_vectors()
        return len(ids)

    # ---------- 问答记录 ----------
    def add_history(self, question: str, answer: str, not_found: bool, context_json: str, cited_json: str,
                    model: str | None, ttft_ms: int | None, latency_ms: int | None) -> int:
        cur = self.conn.execute(
            """INSERT INTO qa_history(owner_id, asked_at, question, answer, not_found, context_json, cited_json,
                                      model, ttft_ms, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (self.owner_id, now_iso(), question, answer, int(not_found), context_json, cited_json, model,
             ttft_ms, latency_ms))
        return int(cur.lastrowid)

    def list_history(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, asked_at, question, not_found FROM qa_history ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def get_history(self, history_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM qa_history WHERE id = ?", (history_id,)).fetchone()
        return dict(row) if row else None

    def delete_history(self, history_id: int) -> None:
        self.conn.execute("DELETE FROM qa_history WHERE id = ?", (history_id,))

    # ---------- 文件 ----------
    @staticmethod
    def _file(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            id=row["id"], source_id=row["source_id"], path=row["path"], file_type=row["file_type"],
            content_hash=row["content_hash"], status=row["status"], error=row["error"],
            chunk_count=row["chunk_count"], size_bytes=row["size_bytes"], mtime=row["mtime"],
            indexed_at=row["indexed_at"],
        )

    def get_file(self, path: str) -> FileRecord | None:
        row = self.conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return self._file(row) if row else None

    def find_missing_by_hash(self, content_hash: str) -> FileRecord | None:
        """同内容、已索引、但旧路径在磁盘上已不存在的文件：视为被移动或改名。"""
        rows = self.conn.execute(
            "SELECT * FROM files WHERE content_hash = ? AND status = 'indexed' ORDER BY id",
            (content_hash,),
        ).fetchall()
        for row in rows:
            if not os.path.exists(row["path"]):
                return self._file(row)
        return None

    def move_file(self, file_id: int, new_path: str, source_id: int, mtime: float) -> None:
        self.conn.execute(
            "UPDATE files SET path = ?, source_id = ?, mtime = ? WHERE id = ?",
            (new_path, source_id, mtime, file_id),
        )

    # ---------- 元数据 ----------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO index_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def touch_file(self, file_id: int, mtime: float) -> None:
        self.conn.execute("UPDATE files SET mtime = ? WHERE id = ?", (mtime, file_id))

    def _upsert_file(self, source_id: int, info: FileInfo, status: str, error: str | None, chunk_count: int) -> int:
        self.conn.execute(
            """INSERT INTO files(source_id, owner_id, path, file_type, content_hash, size_bytes, mtime,
                                 status, error, chunk_count, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 source_id = excluded.source_id, file_type = excluded.file_type,
                 content_hash = excluded.content_hash, size_bytes = excluded.size_bytes,
                 mtime = excluded.mtime, status = excluded.status, error = excluded.error,
                 chunk_count = excluded.chunk_count, indexed_at = excluded.indexed_at""",
            (source_id, self.owner_id, info.path, info.file_type, info.content_hash, info.size_bytes,
             info.mtime, status, error, chunk_count, now_iso(precise=True)),
        )
        return int(self.conn.execute("SELECT id FROM files WHERE path = ?", (info.path,)).fetchone()["id"])

    def _delete_chunks_of(self, file_ids: Sequence[int]) -> None:
        for fid in file_ids:
            ids = [r["id"] for r in self.conn.execute("SELECT id FROM chunks WHERE file_id = ?", (fid,))]
            if ids:
                marks = ",".join("?" * len(ids))
                self.conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", ids)
                self.conn.execute(f"DELETE FROM chunks_vec WHERE chunk_id IN ({marks})", ids)
            self.conn.execute("DELETE FROM chunks WHERE file_id = ?", (fid,))

    def existing_hashes(self, hashes: Sequence[str]) -> set[str]:
        """返回已有向量的文本哈希（Embedding 缓存命中）。"""
        found: set[str] = set()
        uniq = list(dict.fromkeys(hashes))
        for i in range(0, len(uniq), 500):
            part = uniq[i:i + 500]
            marks = ",".join("?" * len(part))
            found.update(r["hash"] for r in self.conn.execute(
                f"SELECT hash FROM vectors WHERE hash IN ({marks})", part))
        return found

    def replace_file_chunks(
        self,
        source_id: int,
        info: FileInfo,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float] | None],
        fts_tokens: Sequence[str],
    ) -> int:
        """单个事务：删旧块（含 fts / vec）→ 写文件记录 → 写新块。返回文件 id。

        vectors 与 chunks 一一对应；某项为 None 表示该文本的向量已在缓存中，直接复用。
        """
        if not (len(chunks) == len(vectors) == len(fts_tokens)):
            raise ValueError("chunks、vectors、fts_tokens 数量必须一致")
        with self.transaction():
            old = self.get_file(info.path)
            if old:
                self._delete_chunks_of([old.id])
            file_id = self._upsert_file(source_id, info, "indexed", None, len(chunks))
            for chunk, vec, tokens in zip(chunks, vectors, fts_tokens):
                loc = chunk.location
                h = text_hash(chunk.text)
                if vec is not None:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO vectors(hash, emb, bits) VALUES (?, ?, vec_quantize_binary(?))",
                        (h, to_int8(vec), sqlite_vec.serialize_float32(list(vec))),
                    )
                cur = self.conn.execute(
                    """INSERT INTO chunks(file_id, owner_id, ordinal, text, lang, page, heading_path,
                                          para_start, para_end, line_start, line_end, text_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (file_id, self.owner_id, chunk.ordinal, chunk.text, chunk.lang, loc.page, loc.heading_path,
                     loc.para_start, loc.para_end, loc.line_start, loc.line_end, h),
                )
                cid = int(cur.lastrowid)
                self.conn.execute("INSERT INTO chunks_fts(rowid, tokens) VALUES (?, ?)", (cid, tokens))
                cur = self.conn.execute(
                    "INSERT INTO chunks_vec(chunk_id, lang, embedding) "
                    "SELECT ?, ?, vec_bit(bits) FROM vectors WHERE hash = ?",
                    (cid, chunk.lang, h),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"文本块缺少向量，且缓存中也没有：{chunk.text[:30]!r}")
        return file_id

    def prune_vectors(self) -> int:
        """删除不再被任何文本块引用的缓存向量，返回删除数量。"""
        cur = self.conn.execute(
            "DELETE FROM vectors WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.text_hash = vectors.hash)")
        return cur.rowcount

    def _mark(self, source_id: int, info: FileInfo, status: str, error: str) -> None:
        with self.transaction():
            old = self.get_file(info.path)
            if old:
                self._delete_chunks_of([old.id])
            self._upsert_file(source_id, info, status, error, 0)

    def mark_failed(self, source_id: int, info: FileInfo, error: str) -> None:
        self._mark(source_id, info, "failed", error)

    def mark_skipped(self, source_id: int, info: FileInfo, reason: str) -> None:
        self._mark(source_id, info, "skipped", reason)

    def remove_files(self, file_ids: Sequence[int]) -> None:
        if not file_ids:
            return
        with self.transaction():
            self._delete_chunks_of(file_ids)
            marks = ",".join("?" * len(file_ids))
            self.conn.execute(f"DELETE FROM files WHERE id IN ({marks})", list(file_ids))

    def files_of_source(self, source_id: int) -> list[FileRecord]:
        rows = self.conn.execute("SELECT * FROM files WHERE source_id = ? ORDER BY id", (source_id,))
        return [self._file(r) for r in rows]

    # ---------- 检索 ----------
    def knn(self, vector: Sequence[float], k: int, lang: str | None = None,
            candidates: int | None = None) -> list[tuple[int, float]]:
        """两级向量检索：二值汉明距离粗筛 candidates 个，再用 int8 余弦距离精排取前 k 个。"""
        n = max(int(candidates or 0), int(k))
        sql = "SELECT chunk_id FROM chunks_vec WHERE embedding MATCH vec_quantize_binary(?) AND k = ?"
        params: list = [sqlite_vec.serialize_float32(list(vector)), n]
        if lang:
            sql += " AND lang = ?"
            params.append(lang)
        ids = [int(r["chunk_id"]) for r in self.conn.execute(sql, params)]
        if not ids:
            return []
        q8 = to_int8(vector)
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"""SELECT c.id AS id, vec_distance_cosine(vec_int8(v.emb), vec_int8(?)) AS d
                FROM chunks c JOIN vectors v ON v.hash = c.text_hash WHERE c.id IN ({marks})""",
            [q8, *ids],
        ).fetchall()
        ranked = sorted(((int(r["id"]), float(r["d"])) for r in rows), key=lambda x: (x[1], x[0]))
        return ranked[:int(k)]

    def fts_search(self, match_query: str, k: int, lang: str | None = None) -> list[tuple[int, float]]:
        if not match_query.strip():
            return []
        sql = (
            "SELECT f.rowid AS id, bm25(chunks_fts) AS score FROM chunks_fts f "
            "JOIN chunks c ON c.id = f.rowid WHERE chunks_fts MATCH ?"
        )
        params: list = [match_query]
        if lang:
            sql += " AND c.lang = ?"
            params.append(lang)
        sql += " ORDER BY score LIMIT ?"
        params.append(int(k))
        return [(int(r["id"]), float(r["score"])) for r in self.conn.execute(sql, params)]

    def langs_of(self, chunk_ids: Sequence[int]) -> dict[int, str]:
        if not chunk_ids:
            return {}
        marks = ",".join("?" * len(chunk_ids))
        return {int(r["id"]): r["lang"] for r in self.conn.execute(
            f"SELECT id, lang FROM chunks WHERE id IN ({marks})", list(chunk_ids))}

    def load_chunks(self, chunk_ids: Sequence[int]) -> list[RetrievedChunk]:
        """按传入顺序返回，附带文件路径与定位信息。"""
        if not chunk_ids:
            return []
        marks = ",".join("?" * len(chunk_ids))
        rows = self.conn.execute(
            f"""SELECT c.*, f.path AS file_path, f.file_type AS file_type
                FROM chunks c JOIN files f ON f.id = c.file_id WHERE c.id IN ({marks})""",
            list(chunk_ids),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        out = []
        for cid in chunk_ids:
            r = by_id.get(cid)
            if r is None:
                continue
            out.append(self._retrieved(r))
        return out

    @staticmethod
    def _retrieved(r: sqlite3.Row) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=r["id"], file_path=r["file_path"], file_type=r["file_type"], text=r["text"],
            lang=r["lang"], file_id=r["file_id"], ordinal=r["ordinal"], ordinal_end=r["ordinal"],
            location=Location(page=r["page"], heading_path=r["heading_path"], para_start=r["para_start"],
                              para_end=r["para_end"], line_start=r["line_start"], line_end=r["line_end"]),
        )

    def neighbors(self, file_id: int, ordinals: Sequence[int]) -> list[RetrievedChunk]:
        """按文件内序号取文本块（补入相邻片段用），按序号排序返回。"""
        if not ordinals:
            return []
        marks = ",".join("?" * len(ordinals))
        rows = self.conn.execute(
            f"""SELECT c.*, f.path AS file_path, f.file_type AS file_type
                FROM chunks c JOIN files f ON f.id = c.file_id
                WHERE c.file_id = ? AND c.ordinal IN ({marks}) ORDER BY c.ordinal""",
            [file_id, *ordinals],
        ).fetchall()
        return [self._retrieved(r) for r in rows]

    def files_of_chunks(self, chunk_ids: Sequence[int]) -> dict[int, int]:
        """chunk_id → file_id（文档排序用）。"""
        if not chunk_ids:
            return {}
        marks = ",".join("?" * len(chunk_ids))
        rows = self.conn.execute(f"SELECT id, file_id FROM chunks WHERE id IN ({marks})", list(chunk_ids)).fetchall()
        return {r["id"]: r["file_id"] for r in rows}

    # ---------- 索引运行 ----------
    def start_run(self, source_id: int) -> IndexRun:
        cur = self.conn.execute(
            "INSERT INTO index_runs(source_id, started_at) VALUES (?, ?)", (source_id, now_iso())
        )
        return IndexRun(id=int(cur.lastrowid), source_id=source_id)

    def finish_run(self, run: IndexRun, aborted: bool = False) -> None:
        run.aborted = aborted
        self.conn.execute(
            """UPDATE index_runs SET finished_at = ?, aborted = ?, files_seen = ?, files_indexed = ?,
                 files_skipped = ?, files_moved = ?, files_failed = ?, files_removed = ?, chunks_added = ?,
                 chunks_reused = ?, parse_ms = ?, embed_ms = ?
               WHERE id = ?""",
            (now_iso(), int(aborted), run.files_seen, run.files_indexed, run.files_skipped, run.files_moved,
             run.files_failed, run.files_removed, run.chunks_added, run.chunks_reused, run.parse_ms,
             run.embed_ms, run.id),
        )

    # ---------- 日志与统计 ----------
    def log_call(self, entry: CallLog) -> None:
        self.conn.execute(
            """INSERT INTO call_logs(owner_id, ts, task, provider, model, latency_ms, ttft_ms, input_tokens,
                                     output_tokens, mem_available_mb, swap_used_mb, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.owner_id, now_iso(), entry.task, entry.provider, entry.model, entry.latency_ms, entry.ttft_ms,
             entry.input_tokens, entry.output_tokens, entry.mem_available_mb, entry.swap_used_mb, entry.error),
        )

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        by_status = {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM files GROUP BY status")}
        by_lang = {r["lang"]: r["n"] for r in self.conn.execute(
            "SELECT lang, COUNT(*) AS n FROM chunks GROUP BY lang")}
        by_type = {r["file_type"]: r["n"] for r in self.conn.execute(
            "SELECT file_type, COUNT(*) AS n FROM files WHERE status = 'indexed' GROUP BY file_type")}
        last = self.conn.execute(
            "SELECT * FROM index_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "sources": q("SELECT COUNT(*) FROM sources"),
            "files_total": q("SELECT COUNT(*) FROM files"),
            "files_by_status": by_status,
            "files_by_type": by_type,
            "chunks_total": q("SELECT COUNT(*) FROM chunks"),
            "vectors_total": q("SELECT COUNT(*) FROM vectors"),
            "chunks_by_lang": by_lang,
            "calls_total": q("SELECT COUNT(*) FROM call_logs"),
            "last_run": dict(last) if last else None,
        }

    def failed_files(self) -> list[FileRecord]:
        rows = self.conn.execute("SELECT * FROM files WHERE status IN ('failed', 'skipped') ORDER BY path")
        return [self._file(r) for r in rows]

    def recent_calls(self, task: str | None = None, limit: int = 1000) -> list[dict]:
        sql, params = "SELECT * FROM call_logs", []
        if task:
            sql += " WHERE task = ?"
            params.append(task)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]
