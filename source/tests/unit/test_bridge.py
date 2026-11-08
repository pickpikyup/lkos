"""窗口应用的后端桥：提问推送、停止、搜索、历史、资料库与后台索引。"""

import time

import pytest

from lkos.gateway.fake import FakeProvider
from lkos.ui.bridge import Bridge
from lkos.ui.window import build_html
from tests.conftest import TEST_DIM, fake_overrides


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def bridge(tmp_path):
    opened = []
    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               opener=lambda p, reveal=False: opened.append((p, reveal)), auto_refresh=False)
    b.opened = opened
    yield b
    b._close()


def types(b):
    return [e["type"] for e in b._sent]


def test_boot_and_html():
    html = build_html()
    assert "/*APP_JS*/" not in html and "window.lkos" in html and "--accent" in html


def test_add_folder_indexes_in_background_then_ask(bridge, corpus):
    info = bridge.boot()
    assert info["stats"]["files"] == 0 and all(m["ok"] for m in info["models"])
    assert bridge.add_folder(str(corpus / "nope"))["error"]
    r = bridge.add_folder(str(corpus))
    assert r["files"] == 4
    assert wait_for(lambda: "library_changed" in types(bridge) and bridge.library()["stats"]["files"] == 4)
    lib = bridge.library()
    assert lib["sources"][0]["files"] == 4 and lib["index"]["last"]["indexed"] == 4

    qid = bridge.ask("项目二期的预算上限是多少？")["qid"]
    assert wait_for(lambda: any(e["type"] == "done" for e in bridge._sent))
    seq = [e["type"] for e in bridge._sent if e.get("qid") == qid]
    assert seq[0] == "sources" and "token" in seq and seq[-1] == "done"
    record = next(e for e in bridge._sent if e["type"] == "done")["record"]
    assert record["context"] and record["cited"] == [1] and record["context"][0]["cited"]
    assert bridge.history()[0]["question"] == "项目二期的预算上限是多少？"
    assert bridge.open_history(record["id"])["answer"] == record["answer"]
    assert bridge.delete_history(record["id"]) == []

    res = bridge.search("retention")["results"]
    assert res and res[0]["n"] == 1 and "loc" in res[0]
    assert bridge.open_file(res[0]["path"]) == {"ok": True} and bridge.reveal(res[0]["path"]) == {"ok": True}
    assert bridge.opened == [(res[0]["path"], False), (res[0]["path"], True)]

    rm = bridge.remove_source(lib["sources"][0]["id"])
    assert rm["removed_files"] == 4 and bridge.library()["sources"] == []


def test_stop_generation_and_busy(tmp_path, corpus):
    slow = FakeProvider("test", dim=TEST_DIM)
    slow.answer_fn = lambda messages: "很长的回答 " * 50 + "[1]"
    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": slow}, auto_refresh=False)
    try:
        b._call(lambda: b._app.indexer.index_folder(corpus))
        b._cancel.set()          # 第一个 token 就停止
        orig = b._cancel.clear
        b._cancel.clear = lambda: None
        qid = b.ask("项目二期的预算上限是多少？")["qid"]
        b._cancel.clear = orig
        assert wait_for(lambda: any(e["type"] == "stopped" for e in b._sent))
        assert not any(e["type"] == "done" for e in b._sent) and b.history() == []
        assert b.ask("  ")["error"]
        assert wait_for(lambda: not b._busy.is_set())
        assert "qid" in b.ask("第二个问题") and qid
    finally:
        b._close()


def test_index_pauses_during_qa_and_manual_pause(bridge, corpus):
    bridge.pause_index()
    bridge.add_folder(str(corpus))
    assert wait_for(lambda: bridge._indexer.state["state"] == "paused" and bridge._indexer.state["reason"] == "manual")
    bridge.resume_index()
    assert wait_for(lambda: bridge.library()["stats"]["files"] == 4)
    assert bridge.update_all()["queued"] == 1
    assert wait_for(lambda: bridge._indexer.state["state"] == "idle" and not bridge._indexer.state["queued"])


@pytest.mark.parametrize("answer,expect", [
    ("资料中没有找到相关信息。", True),
    ("思想领导力是引领他人思考的能力 [1]。资料中没有找到谁能领导的说明。", False),   # 部分回答：照常显示
    ("No relevant information was found in the sources.", True),
])
def test_not_found_only_for_whole_answer(tmp_path, corpus, answer, expect):
    fake = FakeProvider("test", dim=TEST_DIM)
    fake.answer_fn = lambda messages: answer
    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": fake}, auto_refresh=False)
    try:
        b._call(lambda: b._app.indexer.index_folder(corpus))
        b.ask("项目二期的预算上限是多少？")
        assert wait_for(lambda: any(e["type"] == "done" for e in b._sent))
        rec = next(e for e in b._sent if e["type"] == "done")["record"]
        assert rec["not_found"] is expect
    finally:
        b._close()


def test_dev_mode_setting_and_logs(bridge, corpus, tmp_path):
    assert bridge.settings() == {"dev_mode": False, "auto_tags": True, "confirm_online": True}
    assert bridge.set_setting("nope", 1)["error"]
    assert bridge.set_setting("dev_mode", True)["dev_mode"] is True
    assert bridge._load_settings()["dev_mode"] is True            # 写入了 data/ui-settings.json
    start = bridge.logs()["last"]
    bridge._call(lambda: bridge._app.indexer.index_folder(corpus))
    bridge.ask("项目二期的预算上限是多少？")
    assert wait_for(lambda: any(e["type"] == "done" for e in bridge._sent))
    out = bridge.logs(start)
    msgs = [x["msg"] for x in out["lines"]]
    assert any(m.startswith("提问：") for m in msgs) and any(m.startswith("检索 ") for m in msgs)
    assert any("回答完成" in m for m in msgs) and any("[1]" in m and "分数" in m for m in msgs)   # 调试级别：片段明细
    assert out["file"].endswith("lkos.log") and out["last"] >= out["lines"][-1]["seq"]
    assert bridge.logs(out["last"])["lines"] == []
    assert all(x["level"] in ("WARNING", "ERROR") for x in bridge.logs(start, "WARNING")["lines"])
    calls = bridge.calls(10)
    assert calls and {"ttft_ms", "mem_available_mb", "error"} <= set(calls[0])
    assert bridge.open_logs_folder() == {"ok": True}


def test_log_history_and_calls_paging(tmp_path):
    """日志页：日志文件（含滚动出去的旧文件）按时间从早到晚往前翻；模型调用记录从早到晚、可往前翻、取新增。"""
    import logging

    from lkos.logs import read_history

    d = tmp_path / "logs"
    d.mkdir()
    (d / "lkos.log.1").write_text("2026-11-07 10:00:00,001 INFO    lkos.ui: 最早\n"
                                  "2026-11-07 10:00:01,002 DEBUG   lkos.index: 调试\n", "utf-8")
    (d / "lkos.log").write_text("2026-11-08 09:00:00,000 ERROR   lkos.index: 出错\nTraceback (most recent call last):\n"
                                "  File x\n2026-11-08 09:00:05,000 INFO    lkos.ui: 最新\n", "utf-8")
    lines, more = read_history(d, "9999")
    assert [x["msg"].split("\n")[0] for x in lines] == ["最早", "调试", "出错", "最新"] and not more
    assert lines[2]["msg"].endswith("File x") and lines[0]["source"] == "ui" and lines[0]["date"] == "2026-11-07"
    lines, more = read_history(d, "2026-11-08 09:00:05,000", limit=2)
    assert [x["msg"][:2] for x in lines] == ["调试", "出错"] and more
    assert [x["msg"] for x in read_history(d, "9999", logging.INFO)[0]][0] == "最早"
    assert len(read_history(d, "9999", logging.INFO)[0]) == 3


def test_calls_ascending_paging(bridge):
    from lkos.types import CallLog

    repo = bridge._app.repo
    for i in range(5):
        bridge._call(lambda i=i: repo.log_call(CallLog(task="knowledge_qa", provider="test", model=f"m{i}", latency_ms=i)))
    calls = bridge.calls(3)
    assert [c["model"] for c in calls] == ["m2", "m3", "m4"]                   # 最近 3 条，从早到晚
    assert [c["model"] for c in bridge.calls(3, calls[0]["id"])] == ["m0", "m1"]
    assert bridge.calls(10, None, calls[-1]["id"]) == []
