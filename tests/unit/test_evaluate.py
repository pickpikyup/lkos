import pytest

from lkos.app import App
from lkos.errors import ConfigError
from lkos.evaluation.evaluate import evaluate, load_questions, matches
from lkos.gateway.fake import FakeProvider
from tests.conftest import TEST_DIM, fake_overrides

QUESTIONS = """
- question: 项目二期的预算上限是多少？
  expected_files: [项目计划书.docx, 评审会议纪要.pdf]
- question: What is the retention policy for logs?
  expected_files: security-policy.md
- question: Où se trouve la machine à café ?
  expected_files: [fr/guide-accueil.md]
- question: 公司年会在哪里举办？
  expected_files: [年会通知.docx]
"""


def test_load_questions(tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text(QUESTIONS, encoding="utf-8")
    qs = load_questions(p)
    assert len(qs) == 4 and qs[1].expected_files == ["security-policy.md"]


@pytest.mark.parametrize("content", ["foo: bar", "- question: x", "- expected_files: [a]", "[unclosed"])
def test_load_questions_rejects_bad(tmp_path, content):
    p = tmp_path / "q.yaml"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_questions(p)


def test_matches():
    assert matches("/a/b/fr/guide.md", "guide.md")
    assert matches("/a/b/fr/guide.md", "fr/guide.md")
    assert not matches("/a/b/myguide.md", "guide.md")


@pytest.fixture
def app(tmp_path, corpus):
    a = App.open(home=tmp_path / "home", overrides=fake_overrides(),
                 providers={"test": FakeProvider("test", dim=TEST_DIM, invalid_ref=True)})
    a.indexer.index_folder(corpus)
    yield a
    a.close()


def test_retrieval_only_eval(app, tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text(QUESTIONS, encoding="utf-8")
    rep = evaluate(load_questions(p), app.retriever, k=5)
    assert rep.total == 4
    assert [r.hit for r in rep.results] == [True, True, True, False]
    assert rep.hit_rate == 0.75
    assert rep.results[1].rank == 1 and rep.results[3].rank is None
    assert 0 < rep.mrr <= 0.75
    assert rep.citation_rate is None and not rep.with_answer


def test_eval_with_answers(app, tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text(QUESTIONS, encoding="utf-8")
    rep = evaluate(load_questions(p), app.retriever, qa=app.qa, k=5)
    assert rep.with_answer
    assert rep.citation_rate == 1.0
    assert rep.invalid_total == 4  # FakeProvider 每次都附带一个无效编号 [99]
    assert rep.ttft_median_ms is not None


def test_eval_records_per_question_error(app):
    class Boom:
        def ask(self, q, k=5, **_):
            raise RuntimeError("model crashed")

    from lkos.evaluation.evaluate import Question

    rep = evaluate([Question("x", ["a.md"])], app.retriever, qa=Boom())
    assert rep.results[0].error == "model crashed" and rep.hit_rate == 0


def test_eval_lang_option(app, tmp_path):
    from lkos.evaluation.evaluate import Question

    q = [Question("Où se trouve la machine à café ?", ["security-policy.md"])]
    assert not evaluate(q, app.retriever, k=5).results[0].hit          # 按语言过滤：只返回法文块
    assert evaluate(q, app.retriever, k=8, lang="all").results[0].hit   # 不过滤：英文文档也会进入候选
