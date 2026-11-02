"""规模基准：在临时数据库里造 N 个文本块的向量，测两级向量检索的延迟、数据库大小和内存。

只测向量部分（模型不参与），用来回答“100 GB 文档（约 70 万–300 万块）时检索会不会太慢、会不会撑爆内存”。

运行：python scripts/benchmark_scale.py --chunks 1000000
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil  # noqa: E402
import sqlite_vec  # noqa: E402

from lkos.storage.db import connect, init_schema  # noqa: E402
from lkos.storage.repository import Repository  # noqa: E402


def rss_mb() -> int:
    return int(psutil.Process().memory_info().rss / 2**20)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=200_000)
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--candidates", type=int, default=200)
    ap.add_argument("--dir", default=None, help="临时数据库所在目录（默认系统临时目录）")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="lkos-scale-", dir=args.dir))
    db = tmp / "scale.db"
    conn = connect(db)
    init_schema(conn, "bench/e", args.dim)
    repo = Repository(conn)
    nbytes = args.dim // 8

    t = time.perf_counter()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO sources(id, path, created_at) VALUES (1, '/bench', 'now')")
    conn.execute("INSERT INTO files(id, source_id, path, file_type, content_hash, size_bytes, mtime, status) "
                 "VALUES (1, 1, '/bench/f.md', 'md', 'h', 0, 0, 'indexed')")
    batch = 10_000
    for start in range(0, args.chunks, batch):
        n = min(batch, args.chunks - start)
        ids = range(start + 1, start + n + 1)
        conn.executemany("INSERT INTO vectors(hash, emb, bits) VALUES (?, randomblob(?), randomblob(?))",
                         [(f"h{i}", args.dim, nbytes) for i in ids])
        conn.executemany("INSERT INTO chunks(id, file_id, ordinal, text, lang, text_hash) "
                         "VALUES (?, 1, ?, '', ?, ?)",
                         [(i, i, "zh" if i % 2 else "en", f"h{i}") for i in ids])
        conn.executemany("INSERT INTO chunks_vec(chunk_id, lang, embedding) "
                         "SELECT ?, ?, vec_bit(bits) FROM vectors WHERE hash = ?",
                         [(i, "zh" if i % 2 else "en", f"h{i}") for i in ids])
    conn.execute("COMMIT")
    build_s = time.perf_counter() - t
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size_mb = sum(f.stat().st_size for f in tmp.iterdir()) / 2**20

    random.seed(0)
    lat, rss_peak = [], rss_mb()
    for _ in range(args.queries):
        q = [random.gauss(0, 1) for _ in range(args.dim)]
        t = time.perf_counter()
        repo.knn(q, 30, candidates=args.candidates)
        lat.append((time.perf_counter() - t) * 1000)
        rss_peak = max(rss_peak, rss_mb())

    print(f"文本块：{args.chunks:,}　维度：{args.dim}　候选：{args.candidates}")
    print(f"建库：{build_s:.1f}s　数据库：{size_mb:,.0f} MB（约 {size_mb * 1024 / args.chunks:.2f} KB/块，不含正文）")
    print(f"检索延迟：首次 {lat[0]:.0f} ms，之后中位数 {statistics.median(lat[1:]):.0f} ms，最大 {max(lat[1:]):.0f} ms")
    print(f"进程内存峰值：{rss_peak} MB")
    conn.close()
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
