"""后台服务：关掉窗口后继续按各文件夹的周期更新索引（macOS 用 launchd 登录后自动启动）。

- `lkos daemon run`：常驻进程。每 10 分钟看一次哪些文件夹到了更新时间，逐个闲时索引
  （接通电源、电脑空闲、没有在问答时才做，与应用的规则一样）。
- `lkos daemon install / uninstall`：写入或删除 ~/Library/LaunchAgents/com.lkos.indexer.plist。
- `lkos daemon status`：是否已安装、是否在运行、最近一次做了什么。

应用和后台服务不会同时索引：
- 应用打开期间持有 data/app.lock，后台服务看到应用开着就不动手；正在索引时发现应用打开了，
  当前文件做完就停下（下次接着做，已完成的不会重做），由应用接手；
- 真正写索引的一方持有 data/index.lock；应用要索引时如果后台服务还没停下，就等它释放。
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from lkos.errors import Cancelled, LkosError, ModelUnavailableError
from lkos.logs import get_logger

LABEL = "com.lkos.indexer"
CHECK_SECONDS = 600
log = get_logger("daemon")

try:
    import fcntl
except ImportError:  # Windows：没有 flock，退化为“总能拿到锁”
    fcntl = None  # type: ignore[assignment]


# ---------------- 文件锁 ----------------
class FileLock:
    """进程间互斥（flock）。进程退出时系统自动释放，不会留下“死锁文件”。"""

    def __init__(self, path: Path, note: str = ""):
        self.path = Path(path)
        self.note = note
        self._fh = None

    def acquire(self, blocking: bool = False) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except OSError:
                fh.close()
                return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()} {self.note}\n")
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def is_locked_elsewhere(self) -> bool:
        """别的进程是否持有这把锁（自己持有时返回 False）。"""
        if self._fh is not None:
            return False
        if self.acquire():
            self.release()
            return False
        return True

    def __enter__(self):
        self.acquire(blocking=True)
        return self

    def __exit__(self, *exc):
        self.release()


def app_lock(home: Path) -> FileLock:
    return FileLock(Path(home) / "app.lock", "app")


def index_lock(home: Path, who: str) -> FileLock:
    return FileLock(Path(home) / "index.lock", who)


def wait_for_index_lock(lock: FileLock, cancelled: Callable[[], bool] = lambda: False,
                        on_wait: Callable[[], None] | None = None, poll: float = 1.0) -> None:
    """等别的进程释放索引锁；cancelled() 为真时抛出 Cancelled。"""
    told = False
    while not lock.acquire():
        if cancelled():
            raise Cancelled("已停止索引")
        if not told and on_wait:
            on_wait()
            told = True
        time.sleep(poll)


# ---------------- 状态文件 ----------------
def status_path(home: Path) -> Path:
    return Path(home) / "daemon-status.json"


def write_status(home: Path, **kw) -> None:
    p = status_path(home)
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    cur.update(kw, pid=os.getpid(), updated=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------- launchd ----------------
def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(directory: Path | None = None) -> Path:
    return (directory or agents_dir()) / f"{LABEL}.plist"


def plist_bytes(home: Path, python: str, workdir: Path) -> bytes:
    home = Path(home)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "lkos", "--home", str(home), "daemon", "run"],
        "WorkingDirectory": str(workdir),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # 出错退出时重启，正常退出（uninstall）不重启
        "ThrottleInterval": 60,
        "ProcessType": "Background",               # 让 macOS 按后台任务调度（省电、不抢前台）
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(home / "logs" / "daemon.out.log"),
        "StandardErrorPath": str(home / "logs" / "daemon.out.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                 "PYTHONUNBUFFERED": "1"},
    })


def supported() -> bool:
    return sys.platform == "darwin"


def _launchctl(args: list[str], run=subprocess.run) -> tuple[int, str]:
    try:
        p = run(["launchctl", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def install(home: Path, python: str | None = None, workdir: Path | None = None, directory: Path | None = None,
            run=subprocess.run, force: bool = False) -> Path:
    """写入 LaunchAgent 并立即启动。force=True 时在非 macOS 上也只写文件（测试用）。"""
    if not supported() and not force:
        raise LkosError("后台服务目前只支持 macOS（用 launchd 在登录后自动运行）")
    from lkos.config import project_root

    home = Path(home).resolve()
    (home / "logs").mkdir(parents=True, exist_ok=True)
    path = plist_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = plist_bytes(home, python or sys.executable, Path(workdir or project_root() or home.parent))
    domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui"
    if path.exists():
        _launchctl(["bootout", domain, str(path)], run)
    path.write_bytes(data)
    code, out = _launchctl(["bootstrap", domain, str(path)], run)
    if code != 0:
        code, out = _launchctl(["load", "-w", str(path)], run)   # 老版本 macOS
    if code != 0 and not force:
        raise LkosError(f"launchctl 启动后台服务失败：{out.strip() or code}")
    log.info("已安装后台服务：%s", path)
    return path


def uninstall(directory: Path | None = None, run=subprocess.run) -> bool:
    path = plist_path(directory)
    if not path.exists():
        return False
    domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui"
    code, _ = _launchctl(["bootout", domain, str(path)], run)
    if code != 0:
        _launchctl(["unload", "-w", str(path)], run)
    path.unlink()
    log.info("已卸载后台服务：%s", path)
    return True


def status(home: Path, directory: Path | None = None) -> dict:
    try:
        st = json.loads(status_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    running = _alive(st.get("pid")) and st.get("state") not in (None, "exited")
    return {"supported": supported(), "installed": plist_path(directory).exists(), "running": running,
            "state": st.get("state") if running else "stopped", "folder": st.get("folder") if running else None,
            "last": st.get("last"), "updated": st.get("updated")}


# ---------------- 常驻进程 ----------------
class _DaemonGate:
    """闲时闸门 + 应用打开时让出：应用开着就停下当前文件夹（抛 Cancelled），交给应用。"""

    def __init__(self, inner, applock: FileLock, stop: threading.Event, home: Path):
        self.inner = inner
        self.applock = applock
        self.stop = stop
        self.home = home

    def wait(self) -> None:
        while True:
            if self.stop.is_set():
                raise Cancelled("后台服务退出")
            if self.applock.is_locked_elsewhere():
                raise Cancelled("应用已打开，交给应用继续索引")
            reason = self.inner.blocker()
            if reason is None:
                return
            write_status(self.home, state=f"paused:{reason}")
            self.stop.wait(self.inner.cfg.poll_seconds)


def run_daemon(home: Path | None = None, config_path: Path | None = None, check_seconds: float = CHECK_SECONDS,
               once: bool = False, stop: threading.Event | None = None, providers: dict | None = None,
               overrides: dict | None = None, probes: dict | None = None) -> int:
    """常驻循环；once=True 时只检查一轮（测试与手动运行用）。返回本次处理的文件夹数。"""
    from lkos.app import App
    from lkos.indexing.scheduler import due_sources
    from lkos.logs import setup_logging

    stop = stop or threading.Event()
    app = App.open(home=home, config_path=config_path, overrides=overrides, providers=providers)
    home = app.home
    setup_logging(home)
    applock = app_lock(home)
    ilock = index_lock(home, "daemon")
    handled = 0
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    log.info("后台服务启动（进程 %d），每 %d 秒检查一次", os.getpid(), int(check_seconds))
    write_status(home, state="idle", folder=None)
    tried_ollama = False
    try:
        while not stop.is_set():
            if applock.is_locked_elsewhere():
                write_status(home, state="app-open", folder=None)
            else:
                try:
                    from lkos.indexing.relocate import relocate_missing
                    relocate_missing(app.repo)
                except Exception as e:  # noqa: BLE001
                    log.warning("检查改名的文件夹时出错：%s", e)
                for s in due_sources(app.repo.list_sources()):
                    if stop.is_set() or applock.is_locked_elsewhere():
                        break
                    if not ilock.acquire():
                        break   # 应用（或命令行）正在索引
                    try:
                        gate = _DaemonGate(app.index_gate(**(probes or {})), applock, stop, home)
                        write_status(home, state="running", folder=s["path"])
                        log.info("后台服务开始更新 %s", s["path"])
                        rep = app.indexer.index_folder(Path(s["path"]), gate=gate)
                        handled += 1
                        last = {"folder": s["path"], "indexed": rep.files_indexed, "failed": rep.files_failed,
                                "removed": rep.files_removed, "seconds": rep.seconds,
                                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                        log.info("后台服务更新完成 %s：新索引 %d、失败 %d、删除 %d、用时 %.0f 秒", s["path"],
                                 rep.files_indexed, rep.files_failed, rep.files_removed, rep.seconds)
                        write_status(home, state="idle", folder=None, last=last)
                    except Cancelled as e:
                        log.info("后台服务暂停更新 %s：%s", s["path"], e)
                        write_status(home, state="idle", folder=None)
                    except ModelUnavailableError as e:
                        log.warning("后台服务：模型不可用（%s）", e)
                        write_status(home, state="idle", folder=None, last={"folder": s["path"], "error": str(e)})
                        if supported() and not tried_ollama:   # 窗口关掉后 Ollama 可能也被退出了：试着在后台打开
                            tried_ollama = True
                            subprocess.run(["open", "-g", "-a", "Ollama"], capture_output=True)
                        break
                    except LkosError as e:
                        log.error("后台服务更新 %s 出错：%s", s["path"], e)
                        write_status(home, state="idle", folder=None, last={"folder": s["path"], "error": str(e)})
                    finally:
                        ilock.release()
                if not applock.is_locked_elsewhere():
                    write_status(home, state="idle", folder=None)
            if once:
                break
            stop.wait(check_seconds)
    finally:
        write_status(home, state="exited", folder=None)
        app.close()
        log.info("后台服务退出")
    return handled
