import pytest

from lkos.app import App
from lkos.gateway.fake import FakeProvider
from lkos.retrieval.fusion import rrf
from tests.conftest import TEST_DIM, fake_overrides


def test_rrf_scores_and_order():
    out = rrf([[1, 2, 3], [3, 1]], k=60)
    scores = dict(out)
    assert scores[1] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[3] == pytest.approx(1 / 63 + 1 / 61)
    assert scores[2] == pytest.approx(1 / 62)
    assert [d for d, _ in out] == [1, 3, 2]


def test_rrf_ties_keep_first_seen_order_and_empty():
    assert [d for d, _ in rrf([[5], [7]])] == [5, 7]
    assert rrf([[], []]) == []


@pytest.fixture
def app(tmp_path, corpus):
    a = App.open(home=tmp_path / "home", overrides=fake_overrides(),
                 providers={"test": FakeProvider("test", dim=TEST_DIM)})
    a.indexer.index_folder(corpus)
    yield a
    a.close()


def test_chinese_question_finds_budget_and_only_chinese_chunks(app):
    hits = app.retriever.search("项目二期的预算上限是多少？", k=5)
    assert hits[0].file_name in ("项目计划书.docx", "评审会议纪要.pdf")
    assert any("一百二十万" in h.text for h in hits)
    assert {h.lang for h in hits} == {"zh"}
    assert app.retriever.last_debug.lang_filter == "zh"


def test_two_character_keyword_hits_via_fts(app):
    app.retriever.search("招标", k=5)
    fts_ids = app.retriever.last_debug.fts_ids
    texts = [c.text for c in app.repo.load_chunks(fts_ids)]
    assert texts and all("招标" in t for t in texts)


def test_english_and_french_questions(app):
    en = app.retriever.search("What is the retention policy for logs?", k=3)
    assert en[0].file_name == "security-policy.md" and {h.lang for h in en} == {"en"}
    fr = app.retriever.search("Où se trouve la machine à café ?", k=3)
    assert fr[0].file_name == "guide-accueil.md" and {h.lang for h in fr} == {"fr"}


def test_lang_all_disables_filter(app):
    hits = app.retriever.search("budget", k=20, lang="all")
    assert app.retriever.last_debug.lang_filter is None
    assert len({h.lang for h in hits}) > 1


def test_explicit_lang(app):
    hits = app.retriever.search("budget", k=20, lang="zh")
    assert {h.lang for h in hits} == {"zh"}


def test_config_language_filter_all(tmp_path, corpus):
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(retrieval={"language_filter": "all"}),
                 providers={"test": FakeProvider("test", dim=TEST_DIM)})
    a.indexer.index_folder(corpus)
    a.retriever.search("项目预算", k=3)
    assert a.retriever.last_debug.lang_filter is None
    a.close()


def test_scores_descending_and_k_respected(app):
    hits = app.retriever.search("预算", k=2)
    assert len(hits) == 2 and hits[0].score >= hits[1].score


def test_empty_index_returns_nothing(tmp_path):
    a = App.open(home=tmp_path / "empty", overrides=fake_overrides(),
                 providers={"test": FakeProvider("test", dim=TEST_DIM)})
    assert a.retriever.search("预算") == []
    a.close()
