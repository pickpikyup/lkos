"""应用窗口与服务层之间的桥：窗口里的 JavaScript 通过 window.pywebview.api.<方法>() 调用这里的公开方法。

线程模型（设计文档第 3 节的简化版，全部在一个进程里）：
- 问答线程：独占一个 App（数据库连接），处理提问、搜索、历史、资料库查询；同一时间只生成一个回答；
- 索引线程：独占另一个 App，按队列逐个索引文件夹；问答期间（qa.active 标记）、手动暂停时暂停；
  后台自动更新（超过一周没更新的文件夹）还要求接通电源、用户空闲；
- 推送：问答的“找到的资料 / 逐字输出 / 完成”和索引进度，用 window.evaluate_js 调用前端的 lkos.onEvent。

只有不以下划线开头的方法会暴露给前端；返回值都是可 JSON 序列化的 dict / list。
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from lkos.app import App
from lkos.errors import Cancelled, LkosError
from lkos.evaluation.evaluate import is_not_found
from lkos.indexing.scheduler import GateConfig, IndexGate
from lkos.types import Answer, RetrievedChunk

AUTO_REFRESH_DAYS = 7          # 默认每周刷新
AUTO_CHECK_SECONDS = 600       # 每 10 分钟看一次有没有到期的文件夹

PROBLEM_HINTS = {
    "encrypted": "文件有密码，无法读取",
    "no extractable text": "没有文字层（可能是扫描件），以后开启 OCR 后可以检索",
}


def chunk_dict(n: int, c: RetrievedChunk, cited: set[int] | None = None) -> dict:
    return {
        "n": n, "file": c.file_name, "path": c.file_path, "type": c.file_type,
        "loc": c.location.describe(c.file_type), "page": c.location.page,
        "text": c.text, "score": round(c.score or 0.0, 5), "cited": n in (cited or set()),
    }


def system_open(path: str, reveal: bool = False) -> str | None:
    """用系统默认程序打开（reveal=True：在访达中显示）。返回错误信息或 None。"""
    if not Path(path).exists():
        return f"文件已不存在：{path}"
    if sys.platform == "darwin":
        cmd = ["open", "-R", path] if reveal else ["open", path]
    else:
        cmd = ["xdg-open", str(Path(path).parent) if reveal else path]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return f"无法打开：{e}"
    return None


class _Gate:
    """索引闸门：手动暂停、正在问答时暂停；background=True 时还要接通电源、用户空闲（沿用 IndexGate 的探针）。"""

    def __init__(self, worker: "IndexWorker", app: App, background: bool):
        self.w = worker
        self.inner = app.index_gate() if background else None
        self.marker = app.marker

    def blocker(self) -> str | None:
        if self.w._paused.is_set():
            return "manual"
        if self.marker.is_active():
            return "qa"
        return self.inner.blocker() if self.inner is not None else None

    def wait(self) -> None:
        last = None
        while True:
            if self.w._cancel.is_set():
                raise Cancelled("已停止索引")
            reason = self.blocker()
            if reason is None:
                if last is not None:
                    self.w._set(state="running", reason=None)
                return
            if reason != last:
                self.w._set(state="paused", reason=reason)
                last = reason
            time.sleep(0.5)


class IndexWorker:
    """后台索引线程：队列里的文件夹逐个索引，进度推给界面。"""

    def __init__(self, open_app: Callable[[], App], emit: Callable[[dict], None], auto_refresh: bool = True):
        self._open_app = open_app
        self._emit = emit
        self._jobs: queue.Queue = queue.Queue()
        self._paused = threading.Event()
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._queued: list[str] = []
        self._auto = auto_refresh
        self.state: dict = {"state": "idle", "reason": None, "folder": None, "done": 0, "total": 0,
                            "queued": 0, "last": None}
        self._last_emit = 0.0
        self._thread = threading.Thread(target=self._run, name="lkos-index", daemon=True)
        self._stop = threading.Event()
        self._thread.start()

    # ---------- 对外 ----------
    def enqueue(self, folder: str, background: bool = False) -> bool:
        with self._lock:
            if folder in self._queued or (self.state["folder"] == folder and self.state["state"] != "idle"):
                return False
            self._queued.append(folder)
            self.state["queued"] = len(self._queued)
        self._jobs.put((folder, background))
        self._emit_state(force=True)
        return True

    def pause(self) -> None:
        self._paused.set()
        if self.state["state"] != "idle":
            self._set(state="paused", reason="manual")

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self, folder: str | None = None) -> None:
        """停止当前索引（folder 给定时只在正在索引它时停止），并从队列里去掉 folder。"""
        with self._lock:
            if folder is not None and folder in self._queued:
                self._queued.remove(folder)
            current = self.state["folder"]
        if folder is None or folder == current:
            self._cancel.set()

    def shutdown(self) -> None:
        self._stop.set()
        self._cancel.set()
        self._jobs.put(None)

    # ---------- 线程 ----------
    def _set(self, **kw) -> None:
        self.state.update(kw)
        self._emit_state(force=True)

    def _emit_state(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_emit > 0.3:
            self._last_emit = now
            self._emit({"type": "index", **self.state})

    def _run(self) -> None:
        try:
            app = self._open_app()
        except LkosError as e:
            self._set(state="error", reason=str(e))
            return
        try:
            while not self._stop.is_set():
                try:
                    job = self._jobs.get(timeout=AUTO_CHECK_SECONDS)
                except queue.Empty:
                    if self._auto:
                        self._enqueue_stale(app)
                    continue
                if job is None:
                    break
                folder, background = job
                with self._lock:
                    if folder not in self._queued:
                        continue  # 已被移除
                    self._queued.remove(folder)
                    self.state["queued"] = len(self._queued)
                self._index(app, folder, background)
        finally:
            app.close()

    def _enqueue_stale(self, app: App) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=AUTO_REFRESH_DAYS)).isoformat(timespec="seconds")
        for s in app.repo.list_sources():
            if (s["last_indexed"] or "") < cutoff and Path(s["path"]).is_dir():
                self.enqueue(s["path"], background=True)

    def _index(self, app: App, folder: str, background: bool) -> None:
        self._cancel.clear()
        path = Path(folder)
        if not path.is_dir():
            self._set(state="idle", folder=None, last={"folder": folder, "error": "找不到这个文件夹"})
            return
        total = sum(1 for _ in app.indexer.walk(path))
        self._set(state="running", reason=None, folder=folder, done=0, total=total, background=background)

        def on_progress(ev) -> None:
            self.state.update(done=ev.files_seen, file=Path(ev.path).name)
            self._emit_state()

        try:
            rep = app.indexer.index_folder(path, on_progress=on_progress, gate=_Gate(self, app, background))
            last = {"folder": folder, "indexed": rep.files_indexed, "failed": rep.files_failed,
                    "removed": rep.files_removed, "chunks": rep.chunks_added, "seconds": rep.seconds}
        except Cancelled:
            last = {"folder": folder, "stopped": True}
        except LkosError as e:
            last = {"folder": folder, "error": str(e)}
        self._set(state="idle", reason=None, folder=None, done=0, total=0, file=None, last=last)
        self._emit({"type": "library_changed"})


class Bridge:
    """暴露给前端的方法。"""

    def __init__(self, home: Path | None = None, config_path: Path | None = None, overrides: dict | None = None,
                 providers: dict | None = None, opener: Callable[..., str | None] = system_open,
                 auto_refresh: bool = True):
        kw = dict(home=home, config_path=config_path, overrides=overrides, providers=providers)
        self._window = None
        self._opener = opener
        self._qa = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lkos-qa")
        self._app: App = self._qa.submit(lambda: _open(**kw)).result()
        self._cancel = threading.Event()
        self._busy = threading.Event()
        self._indexer = IndexWorker(lambda: App.open(**kw), self._emit, auto_refresh=auto_refresh)
        self._sent: list[dict] = []   # 窗口还没准备好时（以及测试中）记下推送的事件

    # ---------- 内部 ----------
    def _attach(self, window) -> None:
        self._window = window

    def _emit(self, event: dict) -> None:
        self._sent.append(event)
        if len(self._sent) > 500:
            del self._sent[:250]
        w = self._window
        if w is not None:
            try:
                w.evaluate_js(f"window.lkos && window.lkos.onEvent({json.dumps(event, ensure_ascii=False)})")
            except Exception:  # noqa: BLE001 - 窗口关闭过程中推送失败可以忽略
                pass

    def _call(self, fn):
        return self._qa.submit(fn).result()

    def _close(self) -> None:
        self._indexer.shutdown()
        self._cancel.set()
        self._qa.submit(self._app.close).result()
        self._qa.shutdown(wait=False)

    # ---------- 启动 ----------
    def boot(self) -> dict:
        def run():
            s = self._app.repo.stats()
            models = [{"ref": ref, "ok": ok, "message": msg} for ref, ok, msg in self._app.gateway.check()]
            return {"stats": {"sources": s["sources"], "files": s["files_by_status"].get("indexed", 0),
                              "chunks": s["chunks_total"]},
                    "models": models, "model": self._app.cfg.tasks["knowledge_qa"].default_model,
                    "history": self._app.repo.list_history(), "sources": self._app.repo.list_sources(),
                    "index": self._indexer.state}
        out = self._call(run)
        if self._indexer._auto:
            self._qa.submit(lambda: self._indexer._enqueue_stale(self._app))
        return out

    # ---------- 问答 ----------
    def ask(self, question: str) -> dict:
        question = (question or "").strip()
        if not question:
            return {"error": "请输入问题"}
        if self._busy.is_set():
            return {"error": "上一个问题还在回答中"}
        self._busy.set()
        self._cancel.clear()
        qid = f"q{int(time.time() * 1000)}"
        self._qa.submit(self._run_ask, qid, question)
        return {"qid": qid}

    def _run_ask(self, qid: str, question: str) -> None:
        t0 = time.perf_counter()
        sources: list[RetrievedChunk] = []

        def on_context(ctx) -> None:
            sources[:] = ctx
            self._emit({"type": "sources", "qid": qid, "ms": int((time.perf_counter() - t0) * 1000),
                        "sources": [chunk_dict(i, c) for i, c in enumerate(ctx, start=1)]})

        def on_token(t: str) -> None:
            if self._cancel.is_set():
                raise Cancelled("已停止生成")
            self._emit({"type": "token", "qid": qid, "text": t})

        try:
            ans: Answer = self._app.qa.ask(question, on_token=on_token, on_context=on_context)
            record = self._save(question, ans)
            self._emit({"type": "done", "qid": qid, "record": record})
        except Cancelled:
            self._emit({"type": "stopped", "qid": qid})
        except LkosError as e:
            self._emit({"type": "error", "qid": qid, "message": str(e)})
        except Exception as e:  # noqa: BLE001 - 不让后台线程静默死掉
            self._emit({"type": "error", "qid": qid, "message": f"出错了：{e}"})
        finally:
            self._busy.clear()

    def _save(self, question: str, ans: Answer) -> dict:
        cited = sorted({c.ref for c in ans.citations})
        ctx = [chunk_dict(i, c, set(cited)) for i, c in enumerate(ans.context, start=1)]
        # 只有“没有标注任何出处、且整段回答就是一句没找到”才算没找到；
        # 回答了一部分、另一部分说“资料中没有提到”的，照常显示回答
        not_found = bool(ans.no_context) or (not cited and is_not_found(ans.text) and len(ans.text) <= 60)
        model = self._app.cfg.tasks["knowledge_qa"].default_model
        hid = self._app.repo.add_history(question, ans.text, not_found, json.dumps(ctx, ensure_ascii=False),
                                         json.dumps(cited), model, ans.ttft_ms, ans.latency_ms)
        return self._record(self._app.repo.get_history(hid))

    @staticmethod
    def _record(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {"id": row["id"], "question": row["question"], "answer": row["answer"],
                "not_found": bool(row["not_found"]), "context": json.loads(row["context_json"]),
                "cited": json.loads(row["cited_json"]), "model": row["model"], "ttft_ms": row["ttft_ms"],
                "latency_ms": row["latency_ms"], "asked_at": row["asked_at"]}

    def stop(self) -> dict:
        self._cancel.set()
        return {"ok": True}

    def search(self, query: str) -> dict:
        query = (query or "").strip()
        if not query:
            return {"results": []}
        t0 = time.perf_counter()
        hits = self._call(lambda: self._app.retriever.search(query, k=10))
        return {"results": [chunk_dict(i, c) for i, c in enumerate(hits, start=1)],
                "ms": int((time.perf_counter() - t0) * 1000)}

    # ---------- 历史 ----------
    def history(self) -> list[dict]:
        return self._call(self._app.repo.list_history)

    def open_history(self, history_id: int) -> dict | None:
        return self._call(lambda: self._record(self._app.repo.get_history(int(history_id))))

    def delete_history(self, history_id: int) -> list[dict]:
        def run():
            self._app.repo.delete_history(int(history_id))
            return self._app.repo.list_history()
        return self._call(run)

    # ---------- 资料库 ----------
    def library(self) -> dict:
        def run():
            problems = []
            for f in self._app.repo.failed_files():
                err = f.error or ""
                hint = next((v for k, v in PROBLEM_HINTS.items() if err.startswith(k)), None)
                problems.append({"path": f.path, "file": Path(f.path).name, "status": f.status,
                                 "hint": hint or (f"无法读取：{err}" if f.status == "failed" else err)})
            s = self._app.repo.stats()
            return {"sources": self._app.repo.list_sources(), "problems": problems,
                    "stats": {"sources": s["sources"], "files": s["files_by_status"].get("indexed", 0),
                              "chunks": s["chunks_total"]},
                    "index": self._indexer.state, "paused": self._indexer._paused.is_set()}
        return self._call(run)

    def choose_folder(self) -> dict:
        """弹出系统的“选择文件夹”对话框。"""
        if self._window is None:
            return {"error": "窗口还没准备好"}
        try:
            import webview
            kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        except ImportError:  # 测试环境：窗口替身自己处理
            kind = "folder"
        picked = self._window.create_file_dialog(kind)
        if not picked:
            return {"cancelled": True}
        return self.add_folder(picked[0] if isinstance(picked, (list, tuple)) else picked)

    def add_folder(self, path: str) -> dict:
        folder = Path(str(path)).expanduser()
        if not folder.is_dir():
            return {"error": f"找不到这个文件夹：{folder}"}
        folder = folder.resolve()
        n = self._call(lambda: sum(1 for _ in self._app.indexer.walk(folder)))
        if n == 0:
            return {"error": "这个文件夹里没有可以索引的文件（PDF、Word .docx、Markdown）"}
        self._call(lambda: self._app.repo.upsert_source(str(folder)))
        self._indexer.enqueue(str(folder))
        self._emit({"type": "library_changed"})
        return {"ok": True, "folder": str(folder), "files": n}

    def update_source(self, path: str) -> dict:
        return {"ok": self._indexer.enqueue(str(path))}

    def update_all(self) -> dict:
        sources = self._call(self._app.repo.list_sources)
        queued = [s["path"] for s in sources if Path(s["path"]).is_dir() and self._indexer.enqueue(s["path"])]
        return {"queued": len(queued)}

    def remove_source(self, source_id: int) -> dict:
        sources = {s["id"]: s for s in self._call(self._app.repo.list_sources)}
        s = sources.get(int(source_id))
        if s is None:
            return {"error": "没有这个文件夹"}
        self._indexer.cancel(s["path"])
        for _ in range(60):  # 等正在进行的索引停下（最多约 30 秒）
            if self._indexer.state["folder"] != s["path"]:
                break
            time.sleep(0.5)
        n = self._call(lambda: self._app.repo.remove_source(s["id"]))
        self._emit({"type": "library_changed"})
        return {"ok": True, "removed_files": n}

    def pause_index(self) -> dict:
        self._indexer.pause()
        return {"paused": True}

    def resume_index(self) -> dict:
        self._indexer.resume()
        return {"paused": False}

    def stop_index(self) -> dict:
        self._indexer.cancel()
        return {"ok": True}

    # ---------- 打开文件 ----------
    def open_file(self, path: str) -> dict:
        problem = self._opener(str(path))
        return {"error": problem} if problem else {"ok": True}

    def reveal(self, path: str) -> dict:
        problem = self._opener(str(path), reveal=True)
        return {"error": problem} if problem else {"ok": True}


def _open(**kw) -> App:
    """打开数据目录；旧格式索引自动备份并换成新格式（与命令行一致）。"""
    from lkos.errors import SchemaVersionError

    try:
        return App.open(**kw)
    except SchemaVersionError:
        _, sources = App.retire_old_index(kw.get("home"))
        app = App.open(**kw)
        for p in sources:
            app.repo.upsert_source(p)
        return app
