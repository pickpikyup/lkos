"""对话：关键词检索、对话串与上下文策略、分支、在线模型的确认 / 排除 / 附加文件、回答语言。"""

import time

import pytest

from lkos.app import App
from lkos.gateway.fake import FakeProvider
from lkos.models_config import save_models
from lkos.qa.history import Turn, conversation_lang, history_messages
from lkos.retrieval.keyword import keyword_search, parse_terms
from lkos.types import Scope
from tests.conftest import TEST_DIM, fake_overrides

ONLINE = "on-cloud/big-model"


class Recording(FakeProvider):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.messages = []

    def chat(self, model, messages, opts, on_token=None):
        self.messages.append(messages)
        user = messages[-1]["content"]
        if "拆成 2–4 个" in user or "Split it into" in user:
            self.answer_fn = lambda m: "1. 预算\n2. 进度"
        elif "检索关键词" in user:
            self.answer_fn = lambda m: "中文：预算\nEnglish: budget\nFrançais: budget"
        else:
            self.answer_fn = None
        return super().chat(model, messages, opts, on_token)


# ---------- 关键词检索 ----------
def test_parse_terms():
    assert parse_terms('二期 预算  "log retention" 二期') == ["二期", "预算", "log retention"]
    assert parse_terms("“二期 预算”") == ["二期 预算"] and parse_terms("  ") == []


def test_keyword_search(tmp_path, corpus):
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        a.indexer.index_folder(corpus)
        r = keyword_search(a.repo, "预算")
        assert r.files and r.total_hits >= 2 and not r.truncated
        names = [f.name for f in r.files]
        assert "项目计划书.docx" in names and "评审会议纪要.pdf" in names
        f = next(f for f in r.files if f.name == "评审会议纪要.pdf")
        assert f.folder == str(corpus.resolve()) and f.hits[0].loc.startswith("第 ") and "预算" in f.hits[0].snippet
        assert keyword_search(a.repo, "预算 不存在的词").files == []                 # 多个词：都要出现
        assert [f.name for f in keyword_search(a.repo, "APPLICATION LOGS").files] == ["security-policy.md"]  # 不区分大小写
        assert [f.name for f in keyword_search(a.repo, "预算", Scope(file_types=["pdf"])).files] == ["评审会议纪要.pdf"]
        assert keyword_search(a.repo, "").files == []
    finally:
        a.close()


# ---------- 上下文策略 ----------
def test_history_policy_and_language():
    turns = [Turn(question=f"问题{i}", answer=("回答很长" * 200) + " [1]") for i in range(5)]
    local = history_messages(turns, online=False, num_ctx=4096)
    assert len(local) == 6 and local[0]["content"] == "问题2"
    assert len(local[1]["content"]) <= 402 and "[1]" not in local[1]["content"]
    online = history_messages(turns, online=True, num_ctx=200000)
    assert len(online) == 10 and online[0]["content"] == "问题0"
    small = history_messages(turns, online=True, num_ctx=3000)
    assert 0 < len(small) < 10 and small[-2]["content"] == "问题4"                 # 太长时丢掉最早的
    assert conversation_lang(["项目预算是多少？"]) == "zh"
    assert conversation_lang(["What is the log retention period?", "And the password rules?"]) == "en"
    assert conversation_lang(["Quels sont les horaires du bureau ?", "Et la machine à café ?"]) == "fr"
    assert conversation_lang(["What is the retention period?", "那密码呢？", "预算？"]) == "zh"


# ---------- 桥接层 ----------
@pytest.fixture
def setup(tmp_path, corpus):
    from lkos.ui.bridge import Bridge

    home = tmp_path / "h"
    save_models(home, {"local": {"chat": "x", "context_window": 4096}, "embedding": {"model": "bge-m3", "dim": 1024},
                       "online": [{"id": "cloud", "name": "云端大模型", "format": "openai", "base_url": "https://cloud.example/v1",
                                   "api_key": "k", "model": "big-model", "context_window": 100000}],
                       "offline_mode": False})
    local, cloud = Recording("test", dim=TEST_DIM), Recording("on-cloud", dim=TEST_DIM)
    b = Bridge(home=home, overrides=fake_overrides(), providers={"test": local, "on-cloud": cloud}, auto_refresh=False)
    b._call(lambda: b._app.indexer.index_folder(corpus))
    yield b, local, cloud, corpus
    b._close()


def done(b, out, timeout=10):
    assert "qid" in out, out
    end = time.time() + timeout
    while time.time() < end:
        for e in b._sent:
            if e.get("qid") == out["qid"] and e["type"] in ("done", "error", "stopped"):
                assert e["type"] == "done", e
                return e["record"]
        time.sleep(0.03)
    raise AssertionError("超时")


def test_thread_follow_up_and_branch(setup):
    b, local, cloud, corpus = setup
    assert b.send("  ")["error"] and b.send("x", mode="bad")["error"]
    r1 = done(b, b.send("项目二期的预算上限是多少？"))
    tid = r1["thread_id"]
    assert tid == r1["id"] and r1["model_name"] == "fake-chat" and not r1["online"]
    r2 = done(b, b.send("那一期呢？", thread_id=tid))
    r3 = done(b, b.send("进度怎么汇报？", thread_id=tid))
    assert r2["thread_id"] == r3["thread_id"] == tid
    msgs = local.messages[-1]
    assert [m["content"] for m in msgs if m["role"] == "user"][:2] == ["项目二期的预算上限是多少？", "那一期呢？"]
    hist = b.history()
    assert hist[0]["thread_id"] == tid and hist[0]["turns"] == 3 and hist[0]["question"] == "项目二期的预算上限是多少？"
    assert [t["question"] for t in b.thread(tid)] == ["项目二期的预算上限是多少？", "那一期呢？", "进度怎么汇报？"]
    # 关键词检索也是对话里的一轮，但不算进模型的上下文
    s = b.send("预算", mode="search", thread_id=tid)["record"]
    assert s["kind"] == "search" and s["extra"]["results"]["files"] and s["model_name"] == "关键词检索"
    r4 = done(b, b.send("还有什么风险？", thread_id=tid))
    assert all("找到" not in m["content"] for m in local.messages[-1][1:-1])
    # 分支：保留到第 2 轮
    br = b.branch(r2["id"])
    assert [t["question"] for t in br["turns"]] == ["项目二期的预算上限是多少？", "那一期呢？"]
    assert br["thread_id"] != tid and br["turns"][0]["branch_of"] == r2["id"]
    assert len(b.thread(tid)) == 5
    rb = done(b, b.send("改问人员安排", thread_id=br["thread_id"]))
    assert rb["thread_id"] == br["thread_id"] and len(b.thread(br["thread_id"])) == 3
    h = {x["thread_id"]: x for x in b.history()}
    assert h[br["thread_id"]]["branch_from"] == "那一期呢？"
    assert b.delete_thread(tid)["deleted"] == 5 and tid not in {x["thread_id"] for x in b.history()}
    assert r4


def test_online_consent_confirm_and_context(setup):
    b, local, cloud, corpus = setup
    choices = b.model_choices()
    assert [c["ref"] for c in choices] == ["test/fake-chat", ONLINE]
    out = b.send("项目二期的预算上限是多少？", model=ONLINE)
    assert out["need_consent"]["name"] == "云端大模型"
    b.consent_online("cloud")
    r1 = done(b, b.send("项目二期的预算上限是多少？"))                         # 本机回答第一轮
    out = b.send("请详细解释预算构成", thread_id=r1["thread_id"], model=ONLINE)
    end = time.time() + 5
    ev = None
    while time.time() < end and ev is None:
        ev = next((e for e in b._sent if e["type"] == "confirm_send" and e["qid"] == out["qid"]), None)
        time.sleep(0.03)
    assert ev and ev["model"] == "云端大模型" and ev["turns"] == 1 and len(ev["items"]) >= 2
    assert not cloud.messages                                                 # 确认之前不发送
    assert b.confirm_send(out["qid"], [1, 2])["ok"]
    r2 = done(b, out)
    assert r2["online"] and r2["model_name"] == "云端大模型" and len(r2["context"]) == 2
    assert r2["extra"]["sent"]["chunks"] == 2 and r2["extra"]["sent"]["turns"] == 1
    msgs = cloud.messages[-1]
    assert msgs[1]["content"] == "项目二期的预算上限是多少？"                  # 整个对话都带上
    assert b.confirm_send("nope", [])["error"]
    # 取消发送
    out = b.send("再问一个", thread_id=r1["thread_id"], model=ONLINE)
    while not any(e["type"] == "confirm_send" and e["qid"] == out["qid"] for e in b._sent):
        time.sleep(0.03)
    b.confirm_send(out["qid"], None)
    end = time.time() + 5
    while time.time() < end and not any(e.get("qid") == out["qid"] and e["type"] == "stopped" for e in b._sent):
        time.sleep(0.03)
    assert any(e.get("qid") == out["qid"] and e["type"] == "stopped" for e in b._sent)


def test_online_rules_folder_block_and_attachments(setup):
    b, local, cloud, corpus = setup
    b.consent_online("cloud")
    b.set_setting("confirm_online", False)
    sid = b.library()["sources"][0]["id"]
    assert b.set_source_settings(sid, {"allow_online": False})["allow_online"] is False
    r = done(b, b.send("项目二期的预算上限是多少？", model=ONLINE))
    assert r["extra"]["sent"]["chunks"] == 0 and r["extra"]["sent"]["excluded"] >= 1
    assert not cloud.messages and r["not_found"]                 # 全部被排除：不调用在线模型
    files = b.find_files("计划书")
    assert files[0]["name"] == "项目计划书.docx" and files[0]["online_ok"] is False and files[0]["tokens"] > 0
    out = b.send("总结一下", model=ONLINE, attachments=[files[0]["path"]])
    end = time.time() + 5
    while time.time() < end and not any(e.get("qid") == out["qid"] and e["type"] == "error" for e in b._sent):
        time.sleep(0.03)
    assert any("不允许发送" in e.get("message", "") for e in b._sent if e.get("qid") == out["qid"])
    b.set_source_settings(sid, {"allow_online": True})
    r = done(b, b.send("总结一下", model=ONLINE, attachments=[files[0]["path"]]))
    n = len(b._call(lambda: b._app.repo.chunks_of_file(files[0]["path"])))
    assert {c["file"] for c in r["context"][:n]} == {"项目计划书.docx"} and r["extra"]["attachments"]   # 全文排在最前
    # 本机模型：附加的文件改为检索范围
    r = done(b, b.send("预算", attachments=[files[0]["path"]]))
    assert {c["file"] for c in r["context"]} == {"项目计划书.docx"}


def test_deep_analysis_in_thread_follows_language(setup):
    b, local, cloud, corpus = setup
    r1 = done(b, b.send("What is the log retention period?", mode="analysis"))
    r2 = done(b, b.send("And how should passwords be rotated?", mode="analysis", thread_id=r1["thread_id"]))
    assert r2["kind"] == "analysis" and r2["thread_id"] == r1["thread_id"]
    system = local.messages[-1][0]["content"]
    assert "Answer in English" in system
    assert any(m["content"] == "What is the log retention period?" for m in local.messages[-1])
    r3 = done(b, b.send("项目二期的预算和进度怎么样？", mode="analysis"))
    assert "用中文回答" in local.messages[-1][0]["content"] and r3["extra"]["keywords"]
