import pytest

from lkos.app import App
from lkos.gateway.fake import FakeProvider
from lkos.qa.citations import validate_citations
from lkos.qa.prompt import (NO_CONTEXT_MESSAGE, SYSTEM_PROMPTS, build_messages, estimate_tokens, fit_context)
from lkos.types import Location, RetrievedChunk
from tests.conftest import TEST_DIM, fake_overrides


def rc(i: int, text: str = "内容", file_type: str = "pdf", **loc) -> RetrievedChunk:
    return RetrievedChunk(chunk_id=i, file_path=f"/d/f{i}.{file_type}", file_type=file_type, text=text,
                          lang="zh", location=Location(**loc))


CTX = [rc(1), rc(2), rc(3)]


# ---------- 引用校验 ----------
@pytest.mark.parametrize("text,clean,valid,invalid", [
    ("预算 120 万 [1]。", "预算 120 万 [1]。", [1], []),
    ("A [1][3] B [2]", "A [1][3] B [2]", [1, 2, 3], []),
    ("A [1, 2] B", "A [1, 2] B", [1, 2], []),
    ("A [1，2、3]", "A [1, 2, 3]", [1, 2, 3], []),
    ("A [4] 结束。", "A 结束。", [], [4]),
    ("A [2, 7] B [0].", "A [2] B.", [2], [7, 0]),
    ("没有引用", "没有引用", [], []),
    ("年份 [2024] 不是编号", "年份 不是编号", [], [2024]),
])
def test_validate_citations(text, clean, valid, invalid):
    out, cites, bad = validate_citations(text, CTX)
    assert out == clean
    assert [c.ref for c in cites] == valid
    assert bad == invalid
    assert all(c.chunk is CTX[c.ref - 1] for c in cites)


def test_validate_with_empty_context():
    out, cites, bad = validate_citations("x [1]", [])
    assert (out, cites, bad) == ("x", [], [1])


# ---------- 提示与预算 ----------
def test_estimate_tokens():
    assert estimate_tokens("预算上限") == 4
    assert estimate_tokens("abcdef") == 2
    assert estimate_tokens("预算 abc") == 2 + 2  # 汉字 2 + ceil(4/3)


def test_fit_context_respects_budget_and_order():
    big = [rc(i, "字" * 3000) for i in range(1, 6)]
    fitted = fit_context("问题", big, "zh", num_ctx=8192, max_output=1024)
    assert [c.chunk_id for c in fitted] == [1, 2]  # 每块约 3000 token，只能放下两块
    assert fit_context("问题", big, "zh", num_ctx=2048, max_output=1024) == []


def test_build_messages_numbers_sources_with_locations():
    ctx = [rc(1, "二期预算上限", file_type="docx", heading_path="第三章 > 3.2", para_start=4, para_end=5),
           rc(2, "评审结论", file_type="pdf", page=2)]
    msgs = build_messages("预算是多少？", ctx, "zh")
    assert msgs[0]["content"] == SYSTEM_PROMPTS["zh"]
    user = msgs[1]["content"]
    assert "[1] (f1.docx · 第三章 > 3.2 · 段落 4–5)\n二期预算上限" in user
    assert "[2] (f2.pdf · 第 2 页)\n评审结论" in user
    assert user.endswith("问题：预算是多少？")


@pytest.mark.parametrize("lang,key", [("en", "en"), ("fr", "fr"), ("other", "en")])
def test_prompt_language(lang, key):
    assert build_messages("q", CTX[:1], lang)[0]["content"] == SYSTEM_PROMPTS[key]


# ---------- QAService ----------
@pytest.fixture
def setup(tmp_path, corpus):
    def make(**fake_kwargs):
        fake = FakeProvider("test", dim=TEST_DIM, **fake_kwargs)
        a = App.open(home=tmp_path / "home", overrides=fake_overrides(), providers={"test": fake})
        a.indexer.index_folder(corpus)
        return a, fake
    return make


def test_ask_returns_answer_with_valid_citation(setup):
    app, fake = setup()
    tokens = []
    ans = app.qa.ask("项目二期的预算上限是多少？", on_token=tokens.append)
    assert not ans.no_context
    assert ans.citations and ans.citations[0].ref == 1
    assert ans.citations[0].chunk is ans.context[0]
    assert "".join(tokens).startswith("根据资料")
    sent = fake.chat_calls[0]["messages"]
    assert sent[0]["content"] == SYSTEM_PROMPTS["zh"]
    assert ans.ttft_ms is not None and ans.input_tokens
    logs = app.repo.recent_calls("knowledge_qa")
    assert len(logs) == 1 and logs[0]["error"] is None
    app.close()


def test_invalid_citation_removed_and_reported(setup):
    app, _ = setup(invalid_ref=True)
    ans = app.qa.ask("项目二期的预算上限是多少？")
    assert ans.invalid_refs == [99]
    assert "[99]" not in ans.text and ans.citations
    app.close()


def test_no_context_skips_model_call(tmp_path):
    fake = FakeProvider("test", dim=TEST_DIM)
    app = App.open(home=tmp_path / "empty", overrides=fake_overrides(), providers={"test": fake})
    ans = app.qa.ask("Where is the coffee machine?")
    assert ans.no_context and ans.text == NO_CONTEXT_MESSAGE["en"]
    assert fake.chat_calls == []
    app.close()


def test_model_override_used(setup):
    app, fake = setup()
    app.qa.ask("预算上限", model_override="test/bigger")
    assert fake.chat_calls[0]["model"] == "bigger"
    app.close()


def test_answer_in_question_language(setup):
    app, fake = setup()
    app.qa.ask("Où se trouve la machine à café ?")
    assert fake.chat_calls[0]["messages"][0]["content"] == SYSTEM_PROMPTS["fr"]
    app.close()


# ---------- 回答语言、部分回答（2026-11-04：用户反馈“总是提示找不到资料”） ----------
@pytest.mark.parametrize("q,lang", [
    ("AI", "zh"), ("5G FWA", "zh"), ("思想领导力是什么", "zh"),
    ("What factors is the national alignment score based on?", "en"),
    ("Où se trouve la machine à café ?", "fr"),
])
def test_answer_lang_short_latin_keywords_default_to_chinese(q, lang):
    from lkos.qa.service import answer_lang

    assert answer_lang(q) == lang


def test_prompt_allows_partial_answers():
    zh = SYSTEM_PROMPTS["zh"]
    assert "逐个回答" in zh and "资料中没有提到" in zh and "只有当所有资料都与问题无关时" in zh
    assert "only if none of the sources" in SYSTEM_PROMPTS["en"].lower()
