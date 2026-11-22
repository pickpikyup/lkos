"""左栏对话历史（搜索、删除）与日志保留（按天清理、全部删除、保留天数设置）。"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from lkos.gateway.fake import FakeProvider
from lkos.logs import calls_cutoff, clear_log_files, log_days, prune_log_files
from lkos.types import CallLog
from tests.conftest import TEST_DIM, fake_overrides


@pytest.fixture
def bridge(tmp_path, corpus):
    from lkos.ui.bridge import Bridge

    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    b._call(lambda: b._app.indexer.index_folder(corpus))
    yield b
    b._close()


def done(b, out, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        for e in b._sent:
            if e.get("qid") == out["qid"] and e["type"] in ("done", "error", "stopped"):
                return e.get("record")
        time.sleep(0.03)
    raise AssertionError("超时")


# ---------- 对话历史 ----------
def test_search_and_delete_threads(bridge):
    r1 = done(bridge, bridge.send("项目二期的预算上限是多少？", mode="qa"))
    r2 = done(bridge, bridge.send("日志保留多久？", mode="qa"))
    done(bridge, bridge.send("那密码规则呢？", mode="qa", thread_id=r2["thread_id"]))
    br = bridge.branch(r1["id"])                                         # 从第一个对话分支
    assert len(bridge.history()) == 3
    assert [h["thread_id"] for h in bridge.search_history("上限是多少")]   # 问题和回答都搜 == [br["thread_id"], r1["thread_id"]]
    assert [h["thread_id"] for h in bridge.search_history("密码 规则")] == [r2["thread_id"]]   # 每个词都要出现
    assert bridge.search_history("密码 上限是多少") == [] and bridge.search_history("不存在的词") == []
    assert len(bridge.search_history("")) == 3
    # 删除原对话：分支是独立的副本，不受影响
    out = bridge.delete_thread(r1["thread_id"])
    assert out["ok"] and out["deleted"] == 1 and [h["thread_id"] for h in out["history"]] == [br["thread_id"], r2["thread_id"]]
    assert len(bridge.thread(br["thread_id"])) == 1
    assert bridge.delete_thread(r1["thread_id"])["error"]               # 已经删掉
    out = bridge.delete_thread(r2["thread_id"])
    assert out["deleted"] == 2 and bridge.thread(r2["thread_id"]) == []
    # 正在生成回答的对话不能删
    bridge._busy.set()
    bridge._active_thread = br["thread_id"]
    try:
        assert "正在生成" in bridge.delete_thread(br["thread_id"])["error"]
    finally:
        bridge._busy.clear()
        bridge._active_thread = None
    assert bridge.delete_thread(br["thread_id"])["ok"] and bridge.history() == []


# ---------- 日志 ----------
def write_log(home, name, entries):
    d = home / "logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("".join(entries), "utf-8")
    return d / name


def stamp(days_ago, now):
    return (now - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def test_prune_by_days_keeps_multiline_entries_and_removes_old_files(tmp_path):
    now = datetime(2026, 11, 22, 22, 0, 0)
    cur = write_log(tmp_path, "lkos.log", [
        f"{stamp(9, now)},001 INFO    lkos.app: 很旧\n",
        f"{stamp(8, now)},002 ERROR   lkos: 出错\nTraceback (most recent call last):\n  File x\n",
        f"{stamp(2, now)},003 INFO    lkos.app: 新的\n",
        f"{stamp(1, now)},004 ERROR   lkos: 新的出错\nTraceback\n  File y\n"])
    old = write_log(tmp_path, "lkos.log.1", [f"{stamp(20, now)},000 INFO    lkos.app: 全部过期\n"])
    launcher = write_log(tmp_path, "launcher.log", [f"== {stamp(10, now)} 启动 ==\n旧输出\n",
                                                    f"== {stamp(3, now)} 启动 ==\n新输出\n"])
    removed = prune_log_files(tmp_path, 7, now=now)
    text = cur.read_text("utf-8")
    assert "很旧" not in text and "出错\nTraceback" not in text.split("新的出错")[0]
    assert "新的\n" in text and "新的出错\nTraceback\n  File y\n" in text            # 多行记录完整保留
    assert not old.exists()                                                           # 整个文件都过期：删除
    assert launcher.read_text("utf-8") == f"== {stamp(3, now)} 启动 ==\n新输出\n"
    assert removed == 4 + 1 + 2
    assert prune_log_files(tmp_path, 7, now=now) == 0                                  # 再清一次没有可删的


def test_clear_logs_and_log_days_setting(bridge):
    home = bridge._app.home
    write_log(home, "lkos.log.2", ["2026-01-01 00:00:00,000 INFO    lkos: x\n"])
    write_log(home, "launcher.log", ["== 2026-11-22 10:00:00 启动 ==\n"])
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    bridge._call(lambda: bridge._app.repo.conn.execute(
        "INSERT INTO call_logs(owner_id, ts, task, provider, model) VALUES (?,?,?,?,?)",
        (bridge._app.repo.owner_id, old_ts, "qa", "test", "m")))
    bridge._call(lambda: bridge._app.repo.log_call(CallLog(task="qa", provider="test", model="m")))
    assert bridge._call(lambda: bridge._app.repo.prune_calls(calls_cutoff(7))) == 1     # 30 天前的那条
    # 保留天数：写进设置，范围 1–365；后台服务读同一个设置
    assert bridge.set_setting("log_days", 3)["log_days"] == 3 and log_days(home) == 3
    assert bridge.set_setting("log_days", 999)["log_days"] == 365
    assert bridge.set_setting("log_days", "abc")["error"]
    assert log_days(home) == 365
    (home / "ui-settings.json").write_text("坏掉的", "utf-8")
    assert log_days(home) == 7
    # 删除全部日志
    r = bridge.clear_logs()
    assert r["ok"] and r["calls"] >= 1
    assert not (home / "logs" / "lkos.log.2").exists() and (home / "logs" / "launcher.log").read_text("utf-8") == ""
    assert bridge._call(lambda: bridge._app.repo.recent_calls(limit=10)) == []
    assert bridge.history() is not None                                             # 问答记录不受影响


def test_clear_log_files_without_files(tmp_path):
    clear_log_files(tmp_path)                    # 没有日志文件也不出错
    assert prune_log_files(tmp_path, 7) == 0
