"""闲时索引：活动标记、暂停闸门、系统探针、最近文件优先。"""

import os
import time

import pytest

from lkos.app import App
from lkos.gateway.fake import FakeProvider
from lkos.indexing import scheduler
from lkos.indexing.scheduler import ActivityMarker, GateConfig, IndexGate
from tests.conftest import TEST_DIM, fake_overrides


# ---------- 活动标记 ----------
def test_marker_nesting_and_cleanup(tmp_path):
    m = ActivityMarker(tmp_path / "qa.active")
    assert not m.is_active()
    with m.active():
        assert m.is_active()
        with m.active():  # 问答里嵌套检索
            assert m.is_active()
        assert m.is_active()  # 内层结束不应删除标记
    assert not m.is_active() and not (tmp_path / "qa.active").exists()


def test_marker_removed_even_on_error(tmp_path):
    m = ActivityMarker(tmp_path / "qa.active")
    with pytest.raises(RuntimeError):
        with m.active():
            raise RuntimeError("boom")
    assert not m.is_active()


def test_stale_marker_is_ignored(tmp_path):
    p = tmp_path / "qa.active"
    p.write_text("123 0")
    old = time.time() - 1000
    os.utime(p, (old, old))
    assert not ActivityMarker(p, stale_seconds=300).is_active()
    assert ActivityMarker(p, stale_seconds=2000).is_active()


# ---------- 闸门 ----------
class Env:
    """可控的“电源 / 空闲 / 时钟”。"""

    def __init__(self, ac=True, idle=999.0):
        self.ac, self.idle, self.slept = ac, idle, 0.0
        self.script = []  # 每次 sleep 后执行的状态变化

    def sleep(self, s):
        self.slept += s
        if self.script:
            self.script.pop(0)(self)


def gate_for(tmp_path, env, **cfg):
    marker = ActivityMarker(tmp_path / "qa.active")
    events = []
    g = IndexGate(GateConfig(**{"idle_seconds": 60, "poll_seconds": 5, **cfg}), marker,
                  on_ac=lambda: env.ac, idle=lambda: env.idle, sleep=env.sleep, on_pause=events.append)
    return g, marker, events


def test_gate_passes_when_idle_on_ac_and_no_qa(tmp_path):
    env = Env()
    g, _, events = gate_for(tmp_path, env)
    g.wait()
    assert env.slept == 0 and events == [] and g.blocker() is None


def test_gate_pauses_during_qa_then_resumes(tmp_path):
    env = Env()
    g, marker, events = gate_for(tmp_path, env)
    marker.path.write_text("1 1")
    env.script = [lambda e: None, lambda e: marker.path.unlink()]  # 两轮后问答结束
    g.wait()
    assert events == ["qa", "resume"] and env.slept == 10 and g.pauses["qa"] == 1


def test_gate_pauses_on_battery_and_when_user_busy(tmp_path):
    env = Env(ac=False, idle=5)
    g, _, events = gate_for(tmp_path, env)
    env.script = [lambda e: setattr(e, "ac", True), lambda e: setattr(e, "idle", 999)]
    g.wait()
    assert events == ["battery", "busy", "resume"]
    assert g.pauses == {"qa": 0, "busy": 1, "battery": 1} and g.paused_seconds == 10


def test_gate_battery_check_can_be_disabled(tmp_path):
    env = Env(ac=False)
    g, _, _ = gate_for(tmp_path, env, require_ac_power=False)
    assert g.blocker() is None


def test_qa_has_priority_over_other_reasons(tmp_path):
    env = Env(ac=False, idle=0)
    g, marker, _ = gate_for(tmp_path, env)
    marker.path.write_text("1 1")
    assert g.blocker() == "qa"


# ---------- macOS 探针 ----------
def test_mac_probes_parse_system_output(monkeypatch):
    outputs = {
        "pmset": "Now drawing from 'AC Power'\n -InternalBattery-0 80%; charging",
        "ioreg": '    | |   "HIDIdleTime" = 125000000000\n',
    }
    monkeypatch.setattr(scheduler, "_run", lambda cmd: outputs[cmd[0]])
    assert scheduler.mac_on_ac_power() is True
    assert scheduler.mac_idle_seconds() == 125.0
    outputs["pmset"] = "Now drawing from 'Battery Power'\n -InternalBattery-0 64%; discharging"
    assert scheduler.mac_on_ac_power() is False


def test_mac_probes_fail_open_without_commands(monkeypatch):
    monkeypatch.setattr(scheduler, "_run", lambda cmd: None)
    assert scheduler.mac_on_ac_power() is True
    assert scheduler.mac_idle_seconds() == float("inf")


# ---------- 与索引、问答的配合 ----------
@pytest.fixture
def app(tmp_path):
    fake = FakeProvider("test", dim=TEST_DIM)
    a = App.open(home=tmp_path / "home", overrides=fake_overrides(), providers={"test": fake})
    yield a, fake
    a.close()


def test_indexer_checks_gate_for_every_file(app, corpus):
    a, _ = app

    class CountingGate:
        calls = 0

        def wait(self):
            CountingGate.calls += 1

    rep = a.indexer.index_folder(corpus, gate=CountingGate())
    assert CountingGate.calls >= rep.files_seen == 4


def test_indexer_waits_while_qa_marker_present(app, corpus, tmp_path):
    a, _ = app
    env = Env()
    gate = a.index_gate(on_ac=lambda: True, idle=lambda: 999.0, sleep=env.sleep)
    a.marker.path.write_text("1 1")
    env.script = [lambda e: a.marker.path.unlink()]
    rep = a.indexer.index_folder(corpus, gate=gate)
    assert rep.files_indexed == 4 and gate.pauses["qa"] == 1 and env.slept == gate.cfg.poll_seconds


def test_qa_and_search_hold_marker_while_running(app, corpus):
    a, fake = app
    a.indexer.index_folder(corpus)
    seen = []
    fake.answer_fn = lambda messages: seen.append(a.marker.is_active()) or "ok [1]"
    a.qa.ask("项目二期的预算上限是多少？")
    assert seen == [True] and not a.marker.is_active()
    a.retriever.search("预算")
    assert not a.marker.is_active()


def test_recent_files_indexed_first(app, tmp_path):
    a, _ = app
    docs = tmp_path / "d"
    docs.mkdir()
    for i, name in enumerate(["old.md", "mid.md", "new.md"]):
        p = docs / name
        p.write_text(f"# {name}\n\ncontent {name}\n", encoding="utf-8")
        os.utime(p, (1_700_000_000 + i * 100, 1_700_000_000 + i * 100))
    order = [p.name for p in a.indexer.walk(docs)]
    assert order == ["new.md", "mid.md", "old.md"]


def test_path_order_option(tmp_path):
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(indexing={"order": "path"}))
    docs = tmp_path / "d"
    docs.mkdir()
    for name in ["b.md", "a.md"]:
        (docs / name).write_text("# x\n\ny\n", encoding="utf-8")
    assert [p.name for p in a.indexer.walk(docs)] == ["a.md", "b.md"]
    a.close()
