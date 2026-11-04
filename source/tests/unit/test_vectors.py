"""v0.2 向量存储：int8 精度、二值粗筛召回、Embedding 缓存、旧版索引检测。"""

import math
import random
import sqlite3

import pytest

from lkos.app import App
from lkos.errors import SchemaVersionError
from lkos.gateway.fake import FakeProvider
from lkos.storage.db import connect, init_schema, set_meta
from lkos.storage.repository import Repository
from lkos.storage.vectors import from_int8, text_hash, to_int8
from lkos.types import Chunk, FileInfo, Location
from tests.conftest import TEST_DIM, fake_overrides


def unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# ---------- 量化 ----------
def test_int8_per_vector_scaling_keeps_cosine():
    random.seed(1)
    for _ in range(20):
        a, b = unit([random.gauss(0, 1) for _ in range(1024)]), unit([random.gauss(0, 1) for _ in range(1024)])
        qa, qb = from_int8(to_int8(a)), from_int8(to_int8(b))
        assert max(abs(x) for x in qa) == 127
        assert abs(cos(qa, qb) - cos(a, b)) < 0.01


def test_int8_zero_vector_and_size():
    assert to_int8([0.0] * 16) == bytes(16)
    assert len(to_int8([0.1] * 1024)) == 1024


def test_text_hash_ignores_whitespace_differences():
    assert text_hash("预算  上限\n一百万") == text_hash("预算 上限 一百万")
    assert text_hash("a") != text_hash("b")


# ---------- 二值粗筛 + int8 精排的召回 ----------
def test_binary_then_int8_recall_on_clustered_vectors(tmp_path):
    """构造 1,500 个 1024 维、带簇结构的向量（比纯随机更接近真实 Embedding），
    检查“二值粗筛 200 个 + int8 精排”与精确余弦 top-10 的平均重合率。"""
    random.seed(7)
    dim, n = 1024, 1500
    centers = [unit([random.gauss(0, 1) for _ in range(dim)]) for _ in range(30)]
    vecs = []
    for i in range(n):
        c = centers[i % 30]
        vecs.append(unit([x + random.gauss(0, 1.0 / math.sqrt(dim)) for x in c]))
    conn = connect(tmp_path / "r.db")
    init_schema(conn, "t/e", dim)
    repo = Repository(conn)
    sid = repo.upsert_source("/d")
    chunks = [Chunk(i, f"text {i}", "en", Location()) for i in range(n)]
    repo.replace_file_chunks(sid, FileInfo("/d/a.md", "md", "h", 1, 1.0), chunks, vecs, ["t"] * n)
    ids = {r["ordinal"]: r["id"] for r in conn.execute("SELECT id, ordinal FROM chunks")}
    overlaps = []
    for qi in range(0, n, 150):
        q = vecs[qi]
        exact = sorted(range(n), key=lambda j: -cos(q, vecs[j]))[:10]
        got = [cid for cid, _ in repo.knn(q, 10, candidates=200)]
        overlaps.append(len({ids[j] for j in exact} & set(got)) / 10)
    conn.close()
    assert sum(overlaps) / len(overlaps) >= 0.9


# ---------- Embedding 缓存 ----------
@pytest.fixture
def setup(tmp_path):
    fake = FakeProvider("test", dim=TEST_DIM)
    app = App.open(home=tmp_path / "home", overrides=fake_overrides(), providers={"test": fake})
    yield app, fake, tmp_path
    app.close()


def embedded(fake):
    return [t for call in fake.embed_calls for t in call]


def test_duplicate_text_embedded_once_across_files(setup):
    app, fake, tmp = setup
    docs = tmp / "docs"
    docs.mkdir()
    footer = "Huawei Proprietary - Restricted Distribution"
    (docs / "a.md").write_text(f"# A\n\nalpha content\n\n## F\n\n{footer}\n", encoding="utf-8")
    (docs / "b.md").write_text(f"# B\n\nbeta content\n\n## F\n\n{footer}\n", encoding="utf-8")
    rep = app.indexer.index_folder(docs)
    assert embedded(fake).count(footer) == 1
    assert rep.chunks_added == 4 and rep.chunks_reused == 1
    assert app.repo.stats()["vectors_total"] == 3


def test_modified_file_only_embeds_changed_chunks(setup):
    app, fake, tmp = setup
    docs = tmp / "docs"
    docs.mkdir()
    md = docs / "a.md"
    md.write_text("# A\n\npart one\n\n## B\n\npart two\n\n## C\n\npart three\n", encoding="utf-8")
    app.indexer.index_folder(docs)
    fake.embed_calls.clear()
    md.write_text("# A\n\npart one\n\n## B\n\npart two CHANGED\n\n## C\n\npart three\n", encoding="utf-8")
    rep = app.indexer.index_folder(docs)
    assert embedded(fake) == ["part two CHANGED"]
    assert rep.chunks_reused == 2 and rep.vectors_pruned == 1  # 旧的 "part two" 向量被清理
    assert app.repo.stats()["vectors_total"] == 3


def test_vectors_pruned_when_files_deleted(setup):
    app, fake, tmp = setup
    docs = tmp / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nonly here\n", encoding="utf-8")
    (docs / "b.md").write_text("# B\n\nstays\n", encoding="utf-8")
    app.indexer.index_folder(docs)
    (docs / "a.md").unlink()
    rep = app.indexer.index_folder(docs)
    assert rep.files_removed == 1 and rep.vectors_pruned == 1
    assert app.repo.stats()["vectors_total"] == 1


def test_index_report_has_timings(setup, corpus):
    app, _, _ = setup
    rep = app.indexer.index_folder(corpus)
    assert rep.parse_seconds >= 0 and rep.embed_seconds >= 0
    run = app.repo.stats()["last_run"]
    assert run["chunks_reused"] == rep.chunks_reused


def test_missing_vector_for_new_text_is_an_error(repo):
    sid = repo.upsert_source("/d")
    with pytest.raises(ValueError, match="缺少向量"):
        repo.replace_file_chunks(sid, FileInfo("/d/a.md", "md", "h", 1, 1.0),
                                 [Chunk(0, "brand new text", "en", Location())], [None], ["t"])


# ---------- 旧版索引 ----------
def test_old_schema_version_requires_rebuild(tmp_path):
    db = tmp_path / "old.db"
    conn = connect(db)
    init_schema(conn, "t/e", TEST_DIM)
    set_meta(conn, "schema_version", "1")
    conn.close()
    conn = connect(db)
    with pytest.raises(SchemaVersionError, match="重新索引"):
        init_schema(conn, "t/e", TEST_DIM)
    conn.close()


def test_dimension_must_be_multiple_of_8(tmp_path):
    from lkos.errors import LkosError

    conn = sqlite3.connect(":memory:")
    with pytest.raises(LkosError, match="8 的倍数"):
        init_schema(conn, "t/e", 100)
