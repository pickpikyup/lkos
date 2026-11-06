"""信息提取、深度分析、限定范围提问、追问：模块与桥接层。"""

import json
import time

import pytest

from lkos.app import App
from lkos.errors import Cancelled, LkosError
from lkos.gateway.fake import FakeProvider
from lkos.qa.analysis import parse_aspects, parse_keywords
from lkos.qa.extract import parse_columns, parse_rows, to_csv, to_markdown
from lkos.types import Scope
from tests.conftest import TEST_DIM, fake_overrides


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------- 纯函数 ----------
def test_parse_columns_and_rows():
    assert parse_columns("列出各项目，字段：名称、预算，负责人。") == ["名称", "预算", "负责人"]
    assert parse_columns("列出所有项目") == []
    text = '好的：\n[{"名称": "硬件", "金额": "四十万", "来源": 2}, {"名称": "", "金额": ""}, {"名称": "软件", "来源": "[1][9]"}]'
    rows = parse_rows(text, ["名称", "金额"], n_sources=3)
    assert rows == [{"values": {"名称": "硬件", "金额": "四十万"}, "refs": [2]},
                    {"values": {"名称": "软件", "金额": ""}, "refs": [1]}]
    md = "| 名称 | 金额 | 来源 |\n| --- | --- | --- |\n| 硬件 | 四十万 | 1 |"
    assert parse_rows(md, ["名称", "金额"], 2) == [{"values": {"名称": "硬件", "金额": "四十万"}, "refs": [1]}]
    table = to_markdown(["名称"], [{"values": {"名称": "a|b"}, "refs": [1, 2]}])
    assert table.splitlines()[-1] == "| a/b | [1][2] |"
    csv = to_csv(["名称"], [{"values": {"名称": "硬件"}, "refs": [1]}], [{"n": 1, "file": "计划.docx", "loc": "第 3 段"}])
    assert csv.startswith("﻿名称,出处文件,出处位置") and "硬件,计划.docx,第 3 段" in csv


def test_parse_keywords():
    assert parse_keywords("中文：预算、进度\n- English: budget, schedule\nFrançais : budget calendrier…\nEnglish: again") == \
        {"zh": "预算 进度", "en": "budget schedule", "fr": "budget calendrier"}
    assert parse_keywords("1. 英文: retention policy\n其他内容") == {"en": "retention policy"}
    assert parse_keywords("没有按格式输出") == {}


def test_parse_aspects():
    assert parse_aspects("1. 预算构成\n2、进度风险\n- 预算构成\n\n· 人员安排\n五、其他\n六、多余") == \
        ["预算构成", "进度风险", "人员安排", "其他"]


# ---------- 模块 ----------
class Scripted(FakeProvider):
    """按提示内容返回：列名 / 提取 JSON / 方面列表 / 分析正文。"""

    def chat(self, model, messages, opts, on_token=None):
        user = messages[-1]["content"]
        if "只输出这张表格的列名" in user:
            self.answer_fn = lambda m: "项目、金额"
        elif "输出 JSON 数组" in user:
            self.answer_fn = lambda m: json.dumps([{"项目": "硬件", "金额": "四十万", "来源": 1},
                                                   {"项目": "软件", "金额": "八十万", "来源": 1},
                                                   {"项目": "硬件", "金额": "四十万", "来源": 1}], ensure_ascii=False)
        elif "检索关键词" in user:
            self.answer_fn = lambda m: "好的：\n中文：预算、进度\nEnglish: budget, schedule\nFrançais : budget, calendrier…"
        elif "拆成 2–4 个" in user:
            self.answer_fn = lambda m: "1. 预算\n2. 进度"
        else:
            self.answer_fn = lambda m: "### 预算\n上限一百二十万 [1]\n### 进度\n分两期 [2]\n### 结论\n可控 [1]"
        return super().chat(model, messages, opts, on_token)


@pytest.fixture
def app(tmp_path, corpus):
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": Scripted("test", dim=TEST_DIM)})
    a.indexer.index_folder(corpus)
    yield a
    a.close()


def test_extract_library_and_file(app, corpus):
    progress = []
    res = app.extractor.extract("列出各项支出的项目和金额", on_progress=lambda d, t: progress.append((d, t)))
    assert res.columns == ["项目", "金额"]                       # 模型给出的列名
    assert [r["values"]["项目"] for r in res.rows] == ["硬件", "软件"]   # 重复行去掉
    assert res.context and progress[-1][0] == progress[-1][1] == res.batches
    path = str((corpus / "项目计划书.docx").resolve())
    res = app.extractor.extract("字段：项目、金额", path=path)
    assert res.columns == ["项目", "金额"] and {c.file_name for c in res.context} == {"项目计划书.docx"}
    with pytest.raises(LkosError):
        app.extractor.extract("字段：项目", path=str(corpus / "没有.docx"))
    with pytest.raises(Cancelled):
        app.extractor.extract("字段：项目", path=path, cancelled=lambda: True)


def test_analysis_aspects_and_citations(app):
    stages, details, tokens = [], {}, []

    def on_progress(stage, done, total, detail=None):
        stages.append(stage)
        details.update(detail or {})

    ans, aspects, info = app.analyzer.analyze("项目二期的预算和进度怎么样？", on_progress=on_progress,
                                              on_token=tokens.append)
    assert aspects == ["预算", "进度"]
    assert stages[0] == "plan" and stages.index("keywords") < stages.index("search") and stages[-1] == "write"
    assert info["keywords"] == {"zh": "预算 进度", "en": "budget schedule", "fr": "budget calendrier"}
    assert set(info["lang_hits"]) == {"zh", "en", "fr"} and all(info["lang_hits"].values())
    assert details["keywords"] == info["keywords"] and details["lang_hits"] == info["lang_hits"]
    names = {c.file_name for c in ans.context}
    assert {"security-policy.md", "guide-accueil.md"} & names                # 英文、法文资料也进了上下文
    assert ans.citations and not ans.invalid_refs and "".join(tokens).startswith("### 预算")
    with pytest.raises(LkosError):
        app.analyzer.analyze("  ")


def test_scope_limits_retrieval(app, corpus):
    pdf_only = Scope(file_types=["pdf"])
    hits = app.retriever.search("项目 预算 会议", k=8, scope=pdf_only)
    assert hits and {h.file_type for h in hits} == {"pdf"}
    one = str((corpus / "项目计划书.docx").resolve())
    hits = app.retriever.search("项目", k=8, scope=Scope(path=one))
    assert hits and {h.file_name for h in hits} == {"项目计划书.docx"}
    assert app.retriever.search("项目", k=8, scope=Scope(since=time.time() + 3600)) == []
    assert Scope.from_dict({"file_types": [], "since": None}).empty


def test_follow_up_uses_history(app):
    prov = app.gateway._providers["test"]
    ans = app.qa.ask("那一期呢？", history=[("项目二期的预算上限是多少？", "一百二十万 [1]")])
    assert ans.context
    msgs = prov.chat_calls[-1]["messages"]
    assert any("项目二期的预算上限" in m["content"] for m in msgs[:-1])     # 上一轮作为对话历史


# ---------- 桥接层 ----------
@pytest.fixture
def bridge(tmp_path, corpus):
    from lkos.ui.bridge import Bridge

    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": Scripted("test", dim=TEST_DIM)},
               auto_refresh=False)
    b._call(lambda: b._app.indexer.index_folder(corpus))
    yield b
    b._close()


def _done(b, qid):
    assert wait_for(lambda: any(e["type"] in ("done", "error") and e["qid"] == qid for e in b._sent))
    ev = next(e for e in b._sent if e["qid"] == qid and e["type"] in ("done", "error"))
    assert ev["type"] == "done", ev
    return ev["record"]


def test_bridge_extract_and_csv(bridge, corpus, tmp_path):
    assert bridge.extract("  ")["error"]
    rec = _done(bridge, bridge.extract("列出各项支出的项目和金额")["qid"])
    assert rec["kind"] == "extract" and rec["extra"]["columns"] == ["项目", "金额"]
    assert len(rec["extra"]["rows"]) == 2 and rec["cited"] == [1] and not rec["not_found"]
    assert rec["answer"].startswith("| 项目 | 金额 | 出处 |")
    assert bridge.history()[0]["kind"] == "extract"
    out = tmp_path / "表.csv"
    assert bridge.export_csv(rec["id"], str(out))["ok"]
    assert out.read_text(encoding="utf-8").startswith("﻿项目,金额,出处文件")
    qa = _done(bridge, bridge.ask("项目二期的预算上限是多少？")["qid"])
    assert bridge.export_csv(qa["id"], str(out))["error"]


def test_bridge_analysis_scope_and_follow_up(bridge, corpus):
    rec = _done(bridge, bridge.analyze("项目二期的预算和进度怎么样？")["qid"])
    assert rec["kind"] == "analysis" and rec["extra"]["aspects"] == ["预算", "进度"] and rec["cited"]
    assert rec["extra"]["keywords"]["en"] == "budget schedule" and set(rec["extra"]["lang_hits"]) == {"zh", "en", "fr"}
    assert any(e["type"] == "progress" and e["stage"] == "keywords" and e["detail"].get("keywords") for e in bridge._sent)
    assert any(e["type"] == "progress" and e["stage"] == "plan" for e in bridge._sent)
    first = _done(bridge, bridge.ask("预算是多少？", scope={"file_types": ["pdf"]})["qid"])
    assert first["extra"]["scope"] == {"file_types": ["pdf"]}
    assert {c["file"].rsplit(".", 1)[-1] for c in first["context"]} == {"pdf"}
    second = _done(bridge, bridge.ask("那一期呢？", follow_up=first["id"])["qid"])
    assert second["extra"]["follow_up"] == first["id"]
    assert bridge.ask("x")["qid"] and bridge.ask("y").get("error")     # 忙时拒绝第二个任务
