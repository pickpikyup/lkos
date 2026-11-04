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
    assert not evaluate(q, app.retriever, k=5, mode="strict").results[0].hit  # 严格模式：只返回法文块
    assert evaluate(q, app.retriever, k=8, lang="all").results[0].hit          # 不区分语言：英文文档也会进入候选


# ---------- 块级命中与答案正确率（M1） ----------
from lkos.evaluation.evaluate import (EvalReport, Question, QuestionResult, answer_correct, chunk_matches,
                                      is_not_found, norm_text)
from lkos.types import Answer, Location, RetrievedChunk


def chunk(path="/d/报告.pdf", text="最小配置为 4 台 X9 服务器", page=3, page_end=None, heading=None):
    return RetrievedChunk(1, path, path.rsplit(".", 1)[-1], text, "zh",
                          Location(page=page, page_end=page_end, heading_path=heading))


def test_load_questions_with_location_and_answer(tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text("""
- question: 最少几台？
  expected_files: 报告.pdf
  expected_text: 最小配置为4台
  expected_page: [3, 4]
  expected_answer: [4台, 四台]
- question: 标题题
  expected_files: [手册.docx]
  expected_heading: 第二章
  expected_answer: 120
""", encoding="utf-8")
    a, b = load_questions(p)
    assert a.expected_text == ["最小配置为4台"] and a.expected_page == [3, 4] and a.expected_answer == ["4台", "四台"]
    assert a.has_location and b.has_location and b.expected_heading == "第二章" and b.expected_answer == ["120"]
    p.write_text("- question: x\n  expected_files: [a]\n  expected_page: 第三页\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_questions(p)
    p.write_text("- question: x\n  expected_files: [a]\n  expected_text: {a: 1}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_questions(p)


def test_norm_text_ignores_width_case_and_whitespace():
    assert norm_text("Ｘ9  服务\n器") == norm_text("x9 服务器") == "x9服务器"


def test_chunk_matches_by_text_page_heading():
    q = Question("q", ["报告.pdf"], expected_text=["最小配置为4台"])
    assert chunk_matches(chunk(), q)                                  # 空白不同也算
    assert not chunk_matches(chunk(path="/d/别的.pdf"), q)            # 文件不对
    assert not chunk_matches(chunk(text="推荐配置为 8 台"), q)
    qp = Question("q", ["报告.pdf"], expected_page=[4])
    assert not chunk_matches(chunk(page=3), qp) and chunk_matches(chunk(page=3, page_end=5), qp)
    qh = Question("q", ["手册.docx"], expected_heading="第二章")
    assert chunk_matches(chunk(path="/d/手册.docx", page=None, heading="手册 > 第二章 > 2.1"), qh)
    assert not chunk_matches(chunk(path="/d/手册.docx", page=None, heading=None), qh)
    assert not chunk_matches(chunk(), Question("q", ["报告.pdf"]))    # 没写位置


@pytest.mark.parametrize("text,expected", [
    ("最少需要 4 台 [1]。", True), ("最少需要四台 [1]", True), ("需要 8 台", False)])
def test_answer_correct(text, expected):
    assert answer_correct(text, ["4台", "四台"]) is expected


@pytest.mark.parametrize("text,expected", [
    ("资料中没有找到相关信息", True), ("No relevant information was found in the sources.", True),
    ("Aucune information pertinente n'a été trouvée", True), ("预算是 120 万 [1]", False)])
def test_is_not_found(text, expected):
    assert is_not_found(text) is expected


def test_report_counts_doc_ok_not_found_and_unsupported():
    def res(hit, not_found, valid, correct=None, chunk_hit=None):
        r = QuestionResult("q", ["a"], [], hit, 1 if hit else None, valid_citations=valid, invalid_citations=0,
                           ttft_ms=1000, not_found=not_found, answer_correct=correct, chunk_hit=chunk_hit, lang="zh")
        return r

    rep = EvalReport(k=4, results=[res(True, True, 0), res(True, False, 0, True, True), res(False, True, 0),
                                   res(True, False, 2, False, False)], with_answer=True)
    assert rep.doc_ok_not_found == 1 and rep.unsupported == 1
    assert rep.answer_accuracy == 0.5 and rep.chunk_hit_rate == 0.5
    d = rep.to_dict()
    assert d["chunk_hit_total"] == 2 and d["ttft_median_by_lang"] == {"zh": 1000}


def test_evaluate_reports_chunk_hit_and_answer(app):
    qs = [Question("项目二期的预算上限是多少？", ["项目计划书.docx", "评审会议纪要.pdf"],
                   expected_text=["二期预算上限为一百二十万元"], expected_answer=["一百二十万"]),
          Question("What is the retention policy for logs?", ["security-policy.md"],
                   expected_text=["this text is not in the corpus"])]
    rep = evaluate(qs, app.retriever, k=4)
    assert [r.chunk_hit for r in rep.results] == [True, False] and rep.chunk_hit_rate == 0.5
    assert rep.results[0].context and rep.results[0].lang == "zh"

    class QA:
        def ask(self, question, **kw):
            hits = app.retriever.search(question, k=4)
            return Answer(text="资料中没有找到相关信息", no_context=False, context=hits)

    rep = evaluate(qs[:1], app.retriever, qa=QA(), k=4)
    r = rep.results[0]
    assert r.hit and r.chunk_hit and r.not_found and r.answer_correct is False
    assert rep.doc_ok_not_found == 1


def test_evaluate_passes_strategy(app):
    q = [Question("项目二期的预算上限是多少？", ["项目计划书.docx"])]
    rep = evaluate(q, app.retriever, k=4, strategy="flat")
    assert rep.strategy == "flat" and app.retriever.last_debug.strategy == "flat"
