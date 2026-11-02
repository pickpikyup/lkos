import math

import pytest

from lkos.errors import EmbeddingMismatchError
from lkos.storage.db import connect, get_meta, init_schema
from lkos.types import CallLog, Chunk, FileInfo, Location
from tests.conftest import TEST_DIM


def vec(*hot: int) -> list[float]:
    v = [0.0] * TEST_DIM
    for i in hot:
        v[i] = 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def info(path: str, h: str = "h1") -> FileInfo:
    return FileInfo(path=path, file_type="md", content_hash=h, size_bytes=10, mtime=1.0)


def chunk(i: int, text: str, lang: str = "zh", **loc) -> Chunk:
    return Chunk(ordinal=i, text=text, lang=lang, location=Location(**loc))


def count(repo, table: str) -> int:
    return repo.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_init_schema_records_embedding_meta(repo):
    meta = get_meta(repo.conn)
    assert meta["embedding_model"] == "test/fake-embed"
    assert meta["embedding_dim"] == str(TEST_DIM)


def test_init_schema_is_idempotent_and_detects_mismatch(tmp_path):
    db = tmp_path / "x.db"
    conn = connect(db)
    init_schema(conn, "a/emb", TEST_DIM)
    init_schema(conn, "a/emb", TEST_DIM)  # 同配置再次初始化不报错
    with pytest.raises(EmbeddingMismatchError):
        init_schema(conn, "a/other", TEST_DIM)
    with pytest.raises(EmbeddingMismatchError):
        init_schema(conn, "a/emb", TEST_DIM * 2)
    conn.close()


def test_replace_file_chunks_writes_all_three_tables(repo):
    sid = repo.upsert_source("/docs")
    assert repo.upsert_source("/docs") == sid
    fid = repo.replace_file_chunks(
        sid, info("/docs/a.md"),
        [chunk(0, "预算 上限", line_start=1, line_end=3), chunk(1, "hello", "en")],
        [vec(0), vec(1)], ["预算 上限", "hello"],
    )
    rec = repo.get_file("/docs/a.md")
    assert rec.id == fid and rec.status == "indexed" and rec.chunk_count == 2
    assert count(repo, "chunks") == count(repo, "chunks_fts") == count(repo, "chunks_vec") == 2


def test_replace_removes_old_rows_from_virtual_tables(repo):
    sid = repo.upsert_source("/docs")
    repo.replace_file_chunks(sid, info("/docs/a.md"), [chunk(0, "旧"), chunk(1, "旧二")], [vec(0), vec(1)], ["旧", "旧二"])
    repo.replace_file_chunks(sid, info("/docs/a.md", "h2"), [chunk(0, "新")], [vec(2)], ["新"])
    assert count(repo, "chunks") == count(repo, "chunks_fts") == count(repo, "chunks_vec") == 1
    assert repo.fts_search('"旧"', 10) == []
    assert repo.get_file("/docs/a.md").content_hash == "h2"


def test_replace_is_atomic_on_error(repo):
    sid = repo.upsert_source("/docs")
    repo.replace_file_chunks(sid, info("/docs/a.md"), [chunk(0, "保留")], [vec(0)], ["保留"])
    with pytest.raises(Exception):
        # 维度错误的向量会在 vec0 插入时失败，整个事务应回滚
        repo.replace_file_chunks(sid, info("/docs/a.md", "h2"), [chunk(0, "坏")], [[1.0, 2.0]], ["坏"])
    assert repo.get_file("/docs/a.md").content_hash == "h1"
    assert [c.text for c in repo.load_chunks([r[0] for r in repo.fts_search('"保留"', 5)])] == ["保留"]


def test_replace_validates_lengths(repo):
    sid = repo.upsert_source("/docs")
    with pytest.raises(ValueError):
        repo.replace_file_chunks(sid, info("/docs/a.md"), [chunk(0, "x")], [], ["x"])


def test_knn_orders_by_distance_and_filters_lang(repo):
    sid = repo.upsert_source("/docs")
    repo.replace_file_chunks(
        sid, info("/docs/a.md"),
        [chunk(0, "zh1", "zh"), chunk(1, "en1", "en"), chunk(2, "zh2", "zh")],
        [vec(0), vec(0, 1), vec(5)], ["a", "b", "c"],
    )
    hits = repo.knn(vec(0), k=3)
    texts = [c.text for c in repo.load_chunks([h[0] for h in hits])]
    assert texts[0] == "zh1" and texts[1] == "en1"
    zh_only = [c.text for c in repo.load_chunks([h[0] for h in repo.knn(vec(0), k=3, lang="zh")])]
    assert zh_only == ["zh1", "zh2"]


def test_fts_search_with_lang_filter(repo):
    sid = repo.upsert_source("/docs")
    repo.replace_file_chunks(
        sid, info("/docs/a.md"),
        [chunk(0, "t", "zh"), chunk(1, "t", "en")], [vec(0), vec(1)], ["预算 上限", "budget 预算"],
    )
    assert len(repo.fts_search('"预算"', 10)) == 2
    assert len(repo.fts_search('"预算"', 10, lang="en")) == 1
    assert repo.fts_search("   ", 10) == []


def test_fts_remove_diacritics(repo):
    sid = repo.upsert_source("/docs")
    repo.replace_file_chunks(sid, info("/docs/a.md"), [chunk(0, "t", "fr")], [vec(0)], ["Le résumé du café"])
    assert len(repo.fts_search('"resume"', 5)) == 1
    assert len(repo.fts_search('"CAFE"', 5)) == 1


def test_load_chunks_preserves_order_and_location(repo):
    sid = repo.upsert_source("/docs")
    repo.replace_file_chunks(
        sid, info("/docs/a.md"),
        [chunk(0, "one", heading_path="A", line_start=1, line_end=2), chunk(1, "two", page=3)],
        [vec(0), vec(1)], ["one", "two"],
    )
    ids = [r[0] for r in repo.conn.execute("SELECT id FROM chunks ORDER BY id")]
    loaded = repo.load_chunks([ids[1], ids[0], 9999])
    assert [c.text for c in loaded] == ["two", "one"]
    assert loaded[1].location == Location(heading_path="A", line_start=1, line_end=2)
    assert loaded[0].location.page == 3 and loaded[0].file_path == "/docs/a.md"


def test_find_missing_by_hash_and_move(repo, tmp_path):
    sid = repo.upsert_source(str(tmp_path))
    gone = str(tmp_path / "old.md")  # 磁盘上不存在
    present = tmp_path / "keep.md"
    present.write_text("x")
    repo.replace_file_chunks(sid, info(str(present), "same"), [chunk(0, "a")], [vec(0)], ["a"])
    assert repo.find_missing_by_hash("same") is None  # 原路径仍存在，不算移动
    repo.replace_file_chunks(sid, info(gone, "moved"), [chunk(0, "b")], [vec(1)], ["b"])
    rec = repo.find_missing_by_hash("moved")
    assert rec and rec.path == gone
    repo.move_file(rec.id, str(tmp_path / "new.md"), sid, 2.0)
    assert repo.get_file(gone) is None
    assert repo.get_file(str(tmp_path / "new.md")).chunk_count == 1


def test_mark_failed_clears_chunks_and_remove_files(repo):
    sid = repo.upsert_source("/docs")
    repo.replace_file_chunks(sid, info("/docs/a.md"), [chunk(0, "a")], [vec(0)], ["a"])
    repo.mark_failed(sid, info("/docs/a.md", "h2"), "boom")
    rec = repo.get_file("/docs/a.md")
    assert rec.status == "failed" and rec.error == "boom" and rec.chunk_count == 0
    assert count(repo, "chunks_vec") == 0
    repo.replace_file_chunks(sid, info("/docs/b.md"), [chunk(0, "b")], [vec(1)], ["b"])
    repo.remove_files([repo.get_file("/docs/b.md").id, rec.id])
    assert count(repo, "files") == count(repo, "chunks") == count(repo, "chunks_fts") == count(repo, "chunks_vec") == 0
    repo.remove_files([])  # 空列表不报错


def test_runs_logs_and_stats(repo):
    sid = repo.upsert_source("/docs")
    run = repo.start_run(sid)
    run.files_seen, run.files_indexed = 3, 2
    repo.finish_run(run)
    repo.replace_file_chunks(sid, info("/docs/a.md"), [chunk(0, "a", "en")], [vec(0)], ["a"])
    repo.mark_skipped(sid, info("/docs/c.md", "h3"), "encrypted")
    repo.log_call(CallLog(task="embed", provider="test", model="m", latency_ms=5))
    s = repo.stats()
    assert s["files_total"] == 2 and s["files_by_status"] == {"indexed": 1, "skipped": 1}
    assert s["chunks_by_lang"] == {"en": 1} and s["calls_total"] == 1
    assert s["last_run"]["files_indexed"] == 2
    assert [f.path for f in repo.failed_files()] == ["/docs/c.md"]
    assert repo.recent_calls("embed")[0]["latency_ms"] == 5
