"""后台服务：文件锁、launchd 配置、常驻循环与应用之间的让路。"""

import plistlib
import threading
import time

import pytest

from lkos import daemon
from lkos.app import App
from lkos.gateway.fake import FakeProvider
from tests.conftest import TEST_DIM, fake_overrides


def test_file_lock_is_exclusive(tmp_path):
    a, b = daemon.FileLock(tmp_path / "x.lock", "a"), daemon.FileLock(tmp_path / "x.lock", "b")
    assert a.acquire() and a.acquire()                 # 重复获取：已持有
    assert not b.acquire() and b.is_locked_elsewhere()
    assert not a.is_locked_elsewhere()
    a.release()
    assert not b.is_locked_elsewhere() and b.acquire()
    assert (tmp_path / "x.lock").read_text().split()[1] == "b"
    b.release()
    with pytest.raises(Exception) as e:
        daemon.wait_for_index_lock(_held(tmp_path), cancelled=lambda: True, poll=0.01)
    assert "停止" in str(e.value)


def _held(tmp_path):
    holder = daemon.FileLock(tmp_path / "y.lock")
    holder.acquire()
    _held.keep = holder                                  # 保持持有
    return daemon.FileLock(tmp_path / "y.lock")


def test_plist_install_uninstall(tmp_path):
    calls = []

    class R:
        returncode = 0
        stdout = stderr = ""

    def run(cmd, **kw):
        calls.append(cmd)
        return R()

    home = tmp_path / "data"
    path = daemon.install(home, python="/x/.venv/bin/python", workdir=tmp_path, directory=tmp_path / "agents",
                          run=run, force=True)
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "com.lkos.indexer" and data["RunAtLoad"] is True
    assert data["ProgramArguments"] == ["/x/.venv/bin/python", "-m", "lkos", "--home", str(home.resolve()), "daemon", "run"]
    assert data["ProcessType"] == "Background" and data["StandardOutPath"].endswith("logs/daemon.out.log")
    assert calls[-1][:2] == ["launchctl", "bootstrap"]
    st = daemon.status(home, directory=tmp_path / "agents")
    assert st["installed"] and not st["running"] and st["state"] == "stopped"
    assert daemon.uninstall(directory=tmp_path / "agents", run=run) and not path.exists()
    assert calls[-1][:2] == ["launchctl", "bootout"]
    assert daemon.uninstall(directory=tmp_path / "agents", run=run) is False


def _probes(**kw):
    return {"on_ac": kw.get("on_ac", lambda: True), "idle": kw.get("idle", lambda: float("inf"))}


def test_run_once_indexes_due_folders(tmp_path, corpus):
    home = tmp_path / "h"
    a = App.open(home=home, overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    a.repo.upsert_source(str(corpus.resolve()))
    a.close()
    kw = dict(home=home, overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    # 应用开着：不动手
    applock = daemon.app_lock(home)
    applock.acquire()
    assert daemon.run_daemon(once=True, probes=_probes(), **kw) == 0
    applock.release()
    # 应用关了：更新到期的文件夹
    assert daemon.run_daemon(once=True, probes=_probes(), **kw) == 1
    st = daemon.status(home)
    assert st["last"]["indexed"] == 4 and st["state"] == "stopped"     # once 结束后进程状态为已退出
    # 刚更新过：不再到期
    assert daemon.run_daemon(once=True, probes=_probes(), **kw) == 0


def test_daemon_yields_when_app_opens(tmp_path, corpus):
    home = tmp_path / "h"
    a = App.open(home=home, overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    a.repo.upsert_source(str(corpus.resolve()))
    a.close()
    applock = daemon.app_lock(home)
    n = {"calls": 0}

    def on_ac():                                     # 第二次检查时“应用打开了”
        n["calls"] += 1
        if n["calls"] == 2:
            applock.acquire()
        return True

    handled = daemon.run_daemon(home=home, overrides=fake_overrides(), once=True,
                                providers={"test": FakeProvider("test", dim=TEST_DIM)}, probes=_probes(on_ac=on_ac))
    assert handled == 0 and not daemon.index_lock(home, "t").is_locked_elsewhere()
    applock.release()


def test_daemon_loop_stops_on_event(tmp_path):
    stop = threading.Event()
    t = threading.Thread(target=daemon.run_daemon, kwargs=dict(
        home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
        check_seconds=60, stop=stop))
    t.start()
    end = time.time() + 5
    while time.time() < end and not daemon.status_path(tmp_path / "h").exists():
        time.sleep(0.05)
    assert daemon.status(tmp_path / "h")["running"]
    stop.set()
    t.join(5)
    assert not t.is_alive() and not daemon.status(tmp_path / "h")["running"]


def test_app_waits_for_daemon_index_lock(tmp_path, corpus):
    from lkos.ui.bridge import Bridge

    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    held = daemon.index_lock(tmp_path / "h", "daemon")
    try:
        assert daemon.app_lock(tmp_path / "h").is_locked_elsewhere()      # 应用开着
        held.acquire()
        assert b.add_folder(str(corpus))["ok"]
        end = time.time() + 5
        while time.time() < end and b._indexer.state.get("reason") != "daemon":
            time.sleep(0.05)
        assert b._indexer.state["state"] == "paused" and b._indexer.state["reason"] == "daemon"
        held.release()
        end = time.time() + 10
        while time.time() < end and not (b._indexer.state["state"] == "idle" and b._indexer.state.get("last")):
            time.sleep(0.05)
        assert b._indexer.state["last"]["indexed"] == 4
        assert b.daemon_status()["installed"] in (True, False)
    finally:
        held.release()
        b._close()
    assert not daemon.app_lock(tmp_path / "h").is_locked_elsewhere()      # 关窗后释放
