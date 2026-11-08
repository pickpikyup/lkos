import os
import shutil

import pytest

from lkos.app import App
from lkos.errors import EmbeddingMismatchError, ModelUnavailableError
from lkos.gateway.fake import FakeProvider
from tests.conftest import TEST_DIM, fake_overrides
from tests.samples import make_pdf


@pytest.fixture
def fake():
    return FakeProvider("test", dim=TEST_DIM)


@pytest.fixture
def app(tmp_path, fake):
    a = App.open(home=tmp_path / "home", overrides=fake_overrides(), providers={"test": fake})
    yield a
    a.close()


def embedded_texts(fake):
    return [t for call in fake.embed_calls for t in call]


def test_initial_index_covers_all_formats_and_ignores_others(app, corpus):
    rep = app.indexer.index_folder(corpus)
    assert rep.files_seen == 4  # docx、pdf、两个 md；隐藏文件、锁文件、txt 被忽略
    assert rep.files_indexed == 4 and rep.files_failed == 0
    stats = app.repo.stats()
    assert stats["files_by_type"] == {"docx": 1, "pdf": 1, "md": 2}
    assert set(stats["chunks_by_lang"]) == {"zh", "en", "fr"}
    paths = [r["path"] for r in app.repo.conn.execute("SELECT path FROM files")]
    assert not any(".hidden" in p or "~$" in p or p.endswith(".xyz") for p in paths)


def test_second_run_skips_unchanged_without_embedding(app, corpus, fake):
    app.indexer.index_folder(corpus)
    n = len(fake.embed_calls)
    rep = app.indexer.index_folder(corpus)
    assert (rep.files_skipped, rep.files_indexed) == (4, 0)
    assert len(fake.embed_calls) == n


def test_touched_but_unchanged_file_is_not_reindexed(app, corpus, fake):
    app.indexer.index_folder(corpus)
    md = corpus / "security-policy.md"
    os.utime(md, (1_700_000_000, 1_700_000_000))
    n = len(fake.embed_calls)
    rep = app.indexer.index_folder(corpus)
    assert rep.files_indexed == 0 and len(fake.embed_calls) == n
    assert app.repo.get_file(str(md)).mtime == 1_700_000_000


def test_modified_file_is_reindexed_and_old_chunks_replaced(app, corpus, fake):
    app.indexer.index_folder(corpus)
    md = corpus / "security-policy.md"
    md.write_text("# Security Policy\n\nLogs are now kept for thirty days.\n", encoding="utf-8")
    rep = app.indexer.index_folder(corpus)
    assert rep.files_indexed == 1 and rep.files_skipped == 3
    rec = app.repo.get_file(str(md))
    texts = [r["text"] for r in app.repo.conn.execute("SELECT text FROM chunks WHERE file_id = ?", (rec.id,))]
    assert texts == ["Logs are now kept for thirty days."]
    assert app.repo.fts_search('"ninety"', 10) == []


def test_renamed_file_moves_without_reembedding(app, corpus, fake):
    app.indexer.index_folder(corpus)
    before = app.repo.get_file(str(corpus / "项目计划书.docx"))
    n_embed = len(fake.embed_calls)
    (corpus / "项目计划书.docx").rename(corpus / "项目计划书-v2.docx")
    rep = app.indexer.index_folder(corpus)
    assert rep.files_moved == 1 and rep.files_indexed == 0 and rep.files_removed == 0
    assert len(fake.embed_calls) == n_embed
    after = app.repo.get_file(str(corpus / "项目计划书-v2.docx"))
    assert after.id == before.id and after.chunk_count == before.chunk_count


def test_deleted_file_is_removed_on_full_scan(app, corpus):
    app.indexer.index_folder(corpus)
    (corpus / "fr" / "guide-accueil.md").unlink()
    rep = app.indexer.index_folder(corpus)
    assert rep.files_removed == 1
    assert app.repo.get_file(str(corpus / "fr" / "guide-accueil.md")) is None
    assert "fr" not in app.repo.stats()["chunks_by_lang"]


def test_limit_stops_early_and_skips_cleanup(app, corpus):
    app.indexer.index_folder(corpus)
    shutil.rmtree(corpus / "fr")
    rep = app.indexer.index_folder(corpus, limit=1)
    assert rep.files_seen == 1 and rep.cleanup_done is False and rep.files_removed == 0
    assert app.repo.stats()["files_total"] == 4


def test_failed_and_encrypted_files_recorded_but_do_not_stop_run(app, corpus):
    (corpus / "broken.pdf").write_bytes(b"%PDF-1.4 garbage")
    make_pdf(corpus / "locked.pdf", ["secret"], encrypt_password="pw")
    make_pdf(corpus / "scan.pdf", [""])  # 无文本层
    events = []
    rep = app.indexer.index_folder(corpus, on_progress=events.append)
    assert rep.files_indexed == 4 and rep.files_failed == 1 and rep.files_skipped == 2
    assert rep.failures[0][0].endswith("broken.pdf")
    by_path = {f.path.rsplit("/", 1)[-1]: f for f in app.repo.failed_files()}
    assert by_path["broken.pdf"].status == "failed"
    assert by_path["locked.pdf"].error.startswith("encrypted")
    assert by_path["scan.pdf"].error == "no extractable text"
    assert {e.action for e in events} >= {"indexed", "failed", "skipped"}
    # 失败文件下次会重试；跳过的文件内容不变时不再处理
    rep2 = app.indexer.index_folder(corpus)
    assert rep2.files_failed == 1 and rep2.files_skipped == 6


def test_model_unavailable_aborts_run_and_resumes_later(tmp_path, corpus):
    good = FakeProvider("test", dim=TEST_DIM)
    home = tmp_path / "home"

    class FlakyProvider(FakeProvider):
        calls = 0

        def embed(self, model, texts):
            FlakyProvider.calls += 1
            if FlakyProvider.calls > 2:
                raise ModelUnavailableError("Ollama 挂了")
            return super().embed(model, texts)

    a = App.open(home=home, overrides=fake_overrides(), providers={"test": FlakyProvider("test", dim=TEST_DIM)})
    with pytest.raises(ModelUnavailableError):
        a.indexer.index_folder(corpus)
    done = a.repo.stats()["files_by_status"].get("indexed", 0)
    run = a.repo.conn.execute("SELECT aborted FROM index_runs ORDER BY id DESC").fetchone()
    assert run["aborted"] == 1 and 0 < done < 4
    a.close()

    b = App.open(home=home, overrides=fake_overrides(), providers={"test": good})
    rep = b.indexer.index_folder(corpus)
    assert rep.files_skipped == done and rep.files_indexed == 4 - done
    b.close()


def test_embedding_change_requires_rebuild(tmp_path, corpus):
    home = tmp_path / "home"
    App.open(home=home, overrides=fake_overrides()).close()
    over = fake_overrides()
    over["models"]["embedding"]["ref"] = "test/another-embed"
    with pytest.raises(EmbeddingMismatchError):
        App.open(home=home, overrides=over)


def test_missing_folder(app, tmp_path):
    from lkos.errors import ParseError

    with pytest.raises(ParseError, match="不存在"):
        app.indexer.index_folder(tmp_path / "nope")


def test_large_files_skipped(tmp_path, fake, corpus):
    over = fake_overrides(indexing={"max_file_size_mb": 0.0001})  # 约 100 字节
    a = App.open(home=tmp_path / "h2", overrides=over, providers={"test": fake})
    rep = a.indexer.index_folder(corpus)
    assert rep.files_seen == 0
    a.close()


def test_chunks_carry_locations(app, corpus):
    app.indexer.index_folder(corpus)
    rows = app.repo.conn.execute(
        "SELECT f.file_type, c.page, c.heading_path, c.para_start, c.line_start FROM chunks c "
        "JOIN files f ON f.id = c.file_id").fetchall()
    for r in rows:
        if r["file_type"] == "pdf":
            assert r["page"] in (1, 2)
        elif r["file_type"] == "docx":
            assert r["para_start"] and r["heading_path"].startswith("第三章 预算")
        else:
            assert r["line_start"] and r["heading_path"]


# ---------- 切块参数变化后重新切块（M1） ----------
def _open(home, fake, **indexing):
    return App.open(home=home, overrides=fake_overrides(indexing=indexing), providers={"test": fake})


def test_chunker_change_rechunks_unchanged_files_and_records_signature(tmp_path, corpus, fake):
    home = tmp_path / "home"
    a = _open(home, fake, chunk_max_chars=800)
    first = a.indexer.index_folder(corpus)
    assert not first.rechunked and a.repo.get_meta("chunker:1") == "all800-ov100c-d2"
    assert a.indexer.index_folder(corpus).files_indexed == 0
    a.close()

    b = _open(home, fake, chunk_chars_zh=60, chunk_chars_other=150)
    rep = b.indexer.index_folder(corpus)
    assert rep.rechunked and rep.files_indexed == 4   # 文件没变，但切块参数变了
    assert b.repo.get_meta("chunker:1") == "other150-zh60-ov0.15-d2"
    assert b.repo.get_meta("rechunk:1") is None
    zh_len = b.repo.conn.execute("SELECT MAX(LENGTH(text)) FROM chunks WHERE lang = 'zh'").fetchone()[0]
    assert zh_len <= 60
    again = b.indexer.index_folder(corpus)
    assert not again.rechunked and again.files_indexed == 0
    b.close()


def test_interrupted_rechunk_resumes_without_redoing_finished_files(tmp_path, corpus):
    home = tmp_path / "home"
    good = FakeProvider("test", dim=TEST_DIM)
    a = _open(home, good, chunk_max_chars=800)
    a.indexer.index_folder(corpus)
    a.close()

    class FlakyProvider(FakeProvider):
        calls = 0

        def embed(self, model, texts):
            FlakyProvider.calls += 1
            if FlakyProvider.calls > 2:
                raise ModelUnavailableError("Ollama 挂了")
            return super().embed(model, texts)

    b = _open(home, FlakyProvider("test", dim=TEST_DIM), chunk_chars_zh=50, chunk_chars_other=50, chunk_overlap_ratio=0.3)
    with pytest.raises(ModelUnavailableError):
        b.indexer.index_folder(corpus)
    assert b.repo.get_meta("chunker:1") == "all800-ov100c-d2"   # 中断：签名不更新
    assert b.repo.get_meta("rechunk:1").startswith("other50-zh50-ov0.3-d2@")
    b.close()

    c = _open(home, good, chunk_chars_zh=50, chunk_chars_other=50, chunk_overlap_ratio=0.3)
    rep = c.indexer.index_folder(corpus)
    assert rep.rechunked and 0 < rep.files_indexed < 4 and rep.files_indexed + rep.files_skipped == 4
    assert c.repo.get_meta("chunker:1") == "other50-zh50-ov0.3-d2"
    c.close()


def test_limit_run_does_not_finish_rechunk(tmp_path, corpus, fake):
    home = tmp_path / "home"
    a = _open(home, fake, chunk_max_chars=800)
    a.indexer.index_folder(corpus)
    a.close()
    b = _open(home, fake)
    rep = b.indexer.index_folder(corpus, limit=2)
    assert rep.rechunked and b.repo.get_meta("chunker:1") == "all800-ov100c-d2"
    rest = b.indexer.index_folder(corpus)
    assert rest.rechunked and rest.files_indexed == 2
    assert b.repo.get_meta("chunker:1") == "other1200-zh500-ov0.15-d2"
    b.close()
