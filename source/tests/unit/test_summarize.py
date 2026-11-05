"""文档总结：短文档一次成稿、长文档分段提炼再汇总、出处与停止。"""

import pytest

from lkos.app import App
from lkos.errors import Cancelled, LkosError
from lkos.gateway.fake import FakeProvider
from tests.conftest import TEST_DIM, fake_overrides
from tests.samples import make_pdf


class Recorder(FakeProvider):
    """记录每次调用的提示与输出上限；分段提炼返回固定要点，汇总引用 [1] 和最后一个部分。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    def chat(self, model, messages, opts, on_token=None):
        user = messages[-1]["content"]
        self.calls.append((messages[0]["content"], user, opts.max_tokens))
        if "要点概括" in user or "bullet points" in user:
            self.answer_fn = lambda m: "- 这部分讲了预算与进度"
        else:
            last = max(1, sum(1 for line in user.splitlines() if line.startswith("[") and "]（" in line))
            self.answer_fn = lambda m: f"这份文件介绍了项目情况 [1]。\n- 二期预算上限一百二十万 [1]\n- 结论见最后部分 [{last}]"
        return super().chat(model, messages, opts, on_token)


def open_app(tmp_path, provider, ctx=4096):
    over = fake_overrides()
    over["models"]["chat"]["test/fake-chat"]["context_window"] = ctx
    return App.open(home=tmp_path / "h", overrides=over, providers={"test": provider})


def test_short_document_single_pass_uses_original_text(tmp_path):
    docs = tmp_path / "d"
    make_pdf(docs / "纪要.pdf", ["评审会议纪要：确认二期预算上限一百二十万元。", "会议要求每月汇报进度。"])
    rec = Recorder("test", dim=TEST_DIM)
    a = open_app(tmp_path, rec)
    try:
        a.indexer.index_folder(docs)
        path = str((docs / "纪要.pdf").resolve())
        tokens = []
        ans = a.summarizer.summarize_file(path, on_token=tokens.append)
        assert len(rec.calls) == 1 and rec.calls[0][2] == 700           # 一次成稿，输出上限 700
        assert "各部分原文" in rec.calls[0][1] and "评审会议纪要" in rec.calls[0][1]
        assert ans.citations and ans.context[0].describe().startswith("纪要.pdf · 第 1")
        assert "".join(tokens).strip() and not ans.invalid_refs
        calls = a.repo.recent_calls()
        assert calls[0]["task"] == "summarize"
    finally:
        a.close()


def test_long_document_map_then_reduce(tmp_path):
    docs = tmp_path / "d"
    pages = [f"第 {i} 页：" + "项目二期的建设内容包括服务器采购、网络改造和应用迁移，预算与进度按月汇报。" * 12
             for i in range(1, 13)]
    make_pdf(docs / "长报告.pdf", pages)
    rec = Recorder("test", dim=TEST_DIM)
    a = open_app(tmp_path, rec, ctx=2048)          # 小上下文：必须分段
    try:
        a.indexer.index_folder(docs)
        path = str((docs / "长报告.pdf").resolve())
        progress, ctx_seen = [], []
        ans = a.summarizer.summarize_file(path, on_progress=lambda *p: progress.append(p),
                                          on_context=lambda c: ctx_seen.append(len(c)))
        maps = [c for c in rec.calls if "要点概括" in c[1]]
        finals = [c for c in rec.calls if "各部分要点" in c[1]]
        assert len(maps) >= 2 and all(c[2] == 320 for c in maps) and len(finals) == 1
        assert progress[0] == ("map", 0, len(ans.context)) or progress[0][0] == "map"
        assert progress[-1] == ("final", 0, 1)
        assert ctx_seen and ans.context and all(c.location.page for c in ans.context)
        first, last = ans.context[0], ans.context[-1]
        assert first.location.page == 1 and (last.location.page_end or last.location.page) == 12
        assert {c.ref for c in ans.citations} >= {1}
    finally:
        a.close()


def test_cancel_and_unindexed_file(tmp_path):
    docs = tmp_path / "d"
    make_pdf(docs / "长报告.pdf", ["内容" * 600, "内容" * 600, "内容" * 600])
    a = open_app(tmp_path, Recorder("test", dim=TEST_DIM), ctx=2048)
    try:
        a.indexer.index_folder(docs)
        with pytest.raises(Cancelled):
            a.summarizer.summarize_file(str((docs / "长报告.pdf").resolve()), cancelled=lambda: True)
        with pytest.raises(LkosError):
            a.summarizer.summarize_file(str(docs / "没有.pdf"))
    finally:
        a.close()


def test_bridge_summarize_flow(tmp_path, corpus):
    import time

    from lkos.ui.bridge import Bridge

    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    try:
        b._call(lambda: b._app.indexer.index_folder(corpus))
        assert b.summarize(str(corpus / "没有.pdf"))["error"]
        path = str((corpus / "项目计划书.docx").resolve())
        r = b.summarize(path)
        assert r["question"] == "总结：《项目计划书.docx》"
        end = time.time() + 10
        while time.time() < end and not any(e["type"] in ("done", "error") for e in b._sent):
            time.sleep(0.05)
        done = next(e for e in b._sent if e["type"] == "done")["record"]
        assert done["question"] == "总结：《项目计划书.docx》" and done["context"] and done["cited"]
        assert any(e["type"] == "sources" and e["qid"] == r["qid"] for e in b._sent)
        assert b.history()[0]["question"].startswith("总结：")
    finally:
        b._close()
