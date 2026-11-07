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
import os
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from lkos.app import App
from lkos.errors import Cancelled, LkosError
from lkos.evaluation.evaluate import is_not_found
from lkos.parsers.base import file_type_groups
from lkos.sources import apple_notes
from lkos.models_config import load_models, model_choices
from lkos.ui.chat_api import ChatMixin
from lkos.ui.models_api import ModelsMixin
from lkos.parsers.ocr import available as ocr_available
from lkos.daemon import app_lock, index_lock, wait_for_index_lock
from lkos import daemon as daemon_mod
from lkos.indexing.scheduler import due_sources
from lkos.qa.extract import to_csv, to_markdown
from lkos.indexing.scheduler import GateConfig, IndexGate
from lkos.logs import get_logger, set_level, setup_logging
from lkos.types import Answer, RetrievedChunk, Scope

log = get_logger("app")
ilog = get_logger("index")
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
SETTINGS_FILE = "ui-settings.json"
DEFAULT_SETTINGS = {"dev_mode": False, "auto_tags": True, "confirm_online": True}

AUTO_CHECK_SECONDS = 600       # 每 10 分钟看一次有没有到期的文件夹

PROBLEM_HINTS = {
    "encrypted": "文件有密码，无法读取",
    "no extractable text": "没有文字层（可能是扫描件）：在文件夹的“设置”里打开“文字识别”后可以检索",
    "unsupported": None,   # 用错误信息本身（已写明处理办法）
}


def chunk_dict(n: int, c: RetrievedChunk, cited: set[int] | None = None) -> dict:
    return {
        "n": n, "id": c.chunk_id, "lang": c.lang, "file": c.file_name, "path": c.file_path, "type": c.file_type,
        "loc": c.location.describe(c.file_type), "page": c.location.page,
        "text": c.text, "score": round(c.score or 0.0, 5), "cited": n in (cited or set()),
    }


def _job_cb(cb: dict) -> dict:
    """任务回调里给模型服务用的部分（去掉 qid）。"""
    return {k: v for k, v in cb.items() if k != "qid"}


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
                ilog.info("索引暂停：%s", {"manual": "手动暂停", "qa": "正在问答", "busy": "用户在用电脑",
                                         "battery": "使用电池"}.get(reason, reason))
                self.w._set(state="paused", reason=reason)
                last = reason
            time.sleep(0.5)


class IndexWorker:
    """后台索引线程：队列里的文件夹逐个索引，进度推给界面。"""

    def __init__(self, open_app: Callable[[], App], emit: Callable[[dict], None], auto_refresh: bool = True,
                 home: Path | None = None, auto_tags: Callable[[], bool] = lambda: False):
        self._open_app = open_app
        self._auto_tags = auto_tags
        self._ilock = index_lock(home, "app") if home is not None else None
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
    def enqueue(self, folder: str, background: bool = False, kind: str = "index") -> bool:
        """kind：index（更新索引）/ tags（给还没有标签的文件生成标签，总在后台闲时进行）。"""
        key = folder if kind == "index" else f"{kind}:{folder}"
        with self._lock:
            busy = self.state["folder"] == folder and self.state.get("kind", "index") == kind and self.state["state"] != "idle"
            if key in self._queued or busy:
                return False
            self._queued.append(key)
            self.state["queued"] = sum(1 for k in self._queued if not k.startswith("tags:"))
        self._jobs.put((key, folder, background, kind))
        self._emit_state(force=True)
        return True

    def _index_waiting(self) -> bool:
        with self._lock:
            return any(not k.startswith("tags:") for k in self._queued)

    def pause(self) -> None:
        self._paused.set()
        if self.state["state"] != "idle":
            self._set(state="paused", reason="manual")

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self, folder: str | None = None) -> None:
        """停止当前索引（folder 给定时只在正在索引它时停止），并从队列里去掉 folder。"""
        with self._lock:
            for key in (folder, f"tags:{folder}"):
                if folder is not None and key in self._queued:
                    self._queued.remove(key)
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
                key, folder, background, kind = job
                with self._lock:
                    if key not in self._queued:
                        continue  # 已被移除
                    self._queued.remove(key)
                    self.state["queued"] = sum(1 for k in self._queued if not k.startswith("tags:"))
                if kind == "tags":
                    self._tags(app, folder, background)
                else:
                    self._index(app, folder, background)
        finally:
            app.close()

    def _enqueue_stale(self, app: App) -> None:
        for s in due_sources(app.repo.list_sources()):
            self.enqueue(s["path"], background=True)

    def _index(self, app: App, folder: str, background: bool) -> None:
        self._cancel.clear()
        path = Path(folder)
        if not path.is_dir():
            self._set(state="idle", folder=None, last={"folder": folder, "error": "找不到这个文件夹"})
            return
        total = sum(1 for _ in app.indexer.walk(path))
        ilog.info("开始索引 %s（%d 个文件%s）", folder, total, "，后台自动更新" if background else "")
        self._set(state="running", reason=None, folder=folder, done=0, total=total, background=background, kind="index")

        def on_progress(ev) -> None:
            self.state.update(done=ev.files_seen, file=Path(ev.path).name)
            if ev.action == "failed":
                ilog.warning("索引失败：%s：%s", ev.path, ev.detail)
            elif ev.action == "skipped":
                ilog.info("跳过：%s：%s", Path(ev.path).name, ev.detail)
            else:
                ilog.debug("%s %s", {"indexed": "已索引", "unchanged": "未变化", "moved": "改名/移动"}.get(ev.action, ev.action),
                           Path(ev.path).name)
            self._emit_state()

        try:
            if self._ilock is not None:   # 关窗期间后台服务可能正在索引：等它在当前文件做完后让出
                wait_for_index_lock(self._ilock, self._cancel.is_set,
                                    on_wait=lambda: (ilog.info("后台服务正在索引，等它让出"),
                                                     self._set(state="paused", reason="daemon")),
                                    poll=0.5)
                if self.state["state"] == "paused":
                    self._set(state="running", reason=None)
            rep = app.indexer.index_folder(path, on_progress=on_progress, gate=_Gate(self, app, background))
            last = {"folder": folder, "indexed": rep.files_indexed, "failed": rep.files_failed,
                    "removed": rep.files_removed, "chunks": rep.chunks_added, "seconds": rep.seconds}
            ilog.info("索引完成 %s：新索引 %d、失败 %d、删除 %d、新增文本块 %d（复用向量 %d）、用时 %.1f 秒"
                      "（解析 %.1f 秒、向量 %.1f 秒）%s", folder, rep.files_indexed, rep.files_failed, rep.files_removed,
                      rep.chunks_added, rep.chunks_reused, rep.seconds, rep.parse_seconds, rep.embed_seconds,
                      "，切块参数变化已重新切块" if rep.rechunked else "")
        except Cancelled:
            last = {"folder": folder, "stopped": True}
            ilog.info("已停止索引 %s", folder)
        except LkosError as e:
            last = {"folder": folder, "error": str(e)}
            ilog.error("索引出错 %s：%s", folder, e)
        finally:
            if self._ilock is not None:
                self._ilock.release()
        self._set(state="idle", reason=None, folder=None, done=0, total=0, file=None, last=last)
        self._emit({"type": "library_changed"})
        if "indexed" in last and self._auto_tags():
            self.enqueue(folder, background=True, kind="tags")

    def _tags(self, app: App, folder: str, background: bool = True) -> None:
        """后台闲时给文件生成标签；有新的索引任务排队时先让给它，之后接着做。"""
        self._cancel.clear()
        src = app.repo.source_settings(folder)
        if src is None:
            return
        todo = len(app.repo.files_to_tag(src["id"]))
        if not todo:
            return
        ilog.info("开始生成标签 %s（%d 个文件）", folder, todo)
        self._set(state="running", reason=None, folder=folder, done=0, total=todo, background=background, kind="tags")

        def on_progress(i: int, n: int, path: str) -> None:
            self.state.update(done=i, total=n, file=Path(path).name)
            self._emit_state()

        preempted = False
        try:
            n = app.tagger.run(src["id"], gate=_Gate(self, app, background), on_progress=on_progress,
                               cancelled=lambda: self._cancel.is_set() or self._index_waiting())
            ilog.info("标签生成完成 %s：%d 个文件", folder, n)
            last = {"folder": folder, "tagged": n}
        except Cancelled:
            preempted = not self._cancel.is_set()
            last = {"folder": folder, "stopped": True}
            ilog.info("暂停生成标签 %s%s", folder, "（先更新索引）" if preempted else "")
        except LkosError as e:
            last = {"folder": folder, "error": str(e)}
            ilog.error("生成标签出错 %s：%s", folder, e)
        self._set(state="idle", reason=None, folder=None, done=0, total=0, file=None, kind=None, last_tags=last)
        self._emit({"type": "library_changed"})
        if preempted:
            self.enqueue(folder, background=True, kind="tags")


class Bridge(ChatMixin, ModelsMixin):
    """暴露给前端的方法。"""

    def __init__(self, home: Path | None = None, config_path: Path | None = None, overrides: dict | None = None,
                 providers: dict | None = None, opener: Callable[..., str | None] = system_open,
                 auto_refresh: bool = True):
        kw = dict(home=home, config_path=config_path, overrides=overrides, providers=providers)
        self._kw = kw
        self._auto_refresh = auto_refresh
        self._window = None
        self._opener = opener
        self._qa = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lkos-qa")
        self._app: App = self._qa.submit(lambda: _open(**kw)).result()
        self._ring = setup_logging(self._app.home)
        self._settings = self._load_settings()
        set_level(self._settings["dev_mode"])
        log.info("应用启动：数据目录 %s，问答模型 %s，检索策略 %s", self._app.home,
                 self._app.cfg.tasks["knowledge_qa"].default_model, self._app.cfg.retrieval.strategy)
        self._cancel = threading.Event()
        self._busy = threading.Event()
        # 应用打开期间持有 app.lock：后台服务看到后不再索引，由应用接手
        self._applock = app_lock(self._app.home)
        self._applock.acquire()
        self._indexer = IndexWorker(lambda: App.open(**kw), self._emit, auto_refresh=auto_refresh,
                                    home=self._app.home, auto_tags=lambda: bool(self._settings.get("auto_tags")))
        self._sent: list[dict] = []   # 窗口还没准备好时（以及测试中）记下推送的事件
        self._pending: dict = {}      # 等待用户确认的在线发送（qid → {event, keep}）

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

    def _reload_app(self) -> None:
        """模型配置变了：在问答线程里重新打开（读取新的 models.json）。"""
        def run():
            self._app.close()
            self._app = App.open(**self._kw)
        self._call(run)

    def _restart_indexer(self) -> None:
        self._indexer.shutdown()
        self._indexer._thread.join(10)
        self._indexer = IndexWorker(lambda: App.open(**self._kw), self._emit, auto_refresh=self._auto_refresh,
                                    home=self._app.home, auto_tags=lambda: bool(self._settings.get("auto_tags")))

    def _close(self) -> None:
        self._applock.release()
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
                    "history": self._app.repo.list_history(), "sources": self._sources(),
                    "index": self._indexer.state, "settings": dict(self._settings),
                    "file_types": file_type_groups(list(self._app.cfg.indexing.extensions)),
                    "choices": model_choices(load_models(self._app.home), self._app.cfg)}
        out = self._call(run)
        for m in out["models"]:
            (log.info if m["ok"] else log.warning)("模型 %s：%s", m["ref"], m["message"] or ("就绪" if m["ok"] else "不可用"))
        if self._indexer._auto:
            self._qa.submit(lambda: self._indexer._enqueue_stale(self._app))
        return out

    # ---------- 任务：问答 / 总结 / 深度分析 / 信息提取 ----------
    # 都在问答线程里一个接一个执行，过程推送 sources（找到的资料）/ progress / token / done / stopped / error
    def _start(self, prefix: str, work) -> dict:
        if self._busy.is_set():
            return {"error": "上一个任务还在进行中，请稍等或先停止"}
        self._busy.set()
        self._cancel.clear()
        qid = f"{prefix}{int(time.time() * 1000)}"
        self._qa.submit(self._run_job, qid, work)
        return {"qid": qid}

    def _run_job(self, qid: str, work) -> None:
        t0 = time.perf_counter()

        def on_context(ctx) -> None:
            dbg = self._app.retriever.last_debug
            log.info("检索 %d ms：送入 %d 个片段（问题语言 %s、策略 %s）", int((time.perf_counter() - t0) * 1000),
                     len(ctx), dbg.lang if dbg else "-", dbg.strategy if dbg else "-")
            if dbg:
                log.debug("关键词查询：%s；向量候选 %d、关键词候选 %d", dbg.fts_query or "（无）",
                          len(dbg.vec_ids), len(dbg.fts_ids))
            for i, c in enumerate(ctx, start=1):
                loc = c.location.describe(c.file_type)
                log.debug("  [%d] %s%s · %d 字 · 分数 %.4f", i, c.file_name, f" · {loc}" if loc else "", len(c.text),
                          c.score or 0)
            self._emit({"type": "sources", "qid": qid, "ms": int((time.perf_counter() - t0) * 1000),
                        "sources": [chunk_dict(i, c) for i, c in enumerate(ctx, start=1)]})

        def on_progress(stage: str, done: int, total: int, detail: dict | None = None) -> None:
            log.debug("进度：%s %d/%d%s", stage, done, total, f" {detail}" if detail else "")
            self._emit({"type": "progress", "qid": qid, "stage": stage, "done": done, "total": total,
                        "detail": detail or {}})

        def on_token(t: str) -> None:
            if self._cancel.is_set():
                raise Cancelled("已停止生成")
            self._emit({"type": "token", "qid": qid, "text": t})

        cb = {"on_context": on_context, "on_progress": on_progress, "on_token": on_token,
              "cancelled": self._cancel.is_set, "qid": qid}
        try:
            record = work(cb, t0)
            self._emit({"type": "done", "qid": qid, "record": record})
        except Cancelled:
            log.info("已停止")
            self._emit({"type": "stopped", "qid": qid})
        except LkosError as e:
            log.error("出错：%s", e)
            self._emit({"type": "error", "qid": qid, "message": str(e)})
        except Exception as e:  # noqa: BLE001 - 不让后台线程静默死掉
            log.exception("出错")
            self._emit({"type": "error", "qid": qid, "message": f"出错了：{e}"})
        finally:
            self._busy.clear()

    def ask(self, question: str, scope: dict | None = None, follow_up: int | None = None) -> dict:
        """scope：{"source_ids", "file_types", "since", "path", "tags"}；follow_up：上一轮问答记录的 id（追问）。"""
        question = (question or "").strip()
        if not question:
            return {"error": "请输入问题"}

        def work(cb, t0):
            history = []
            if follow_up:
                prev = self._app.repo.get_history(int(follow_up))
                if prev:
                    history = [(prev["question"], prev["answer"])]
            log.info("提问：%s%s%s", question, f"（追问 #{follow_up}）" if history else "",
                     f"，范围 {scope}" if scope else "")
            ans = self._app.qa.ask(question, on_token=cb["on_token"], on_context=cb["on_context"],
                                   scope=Scope.from_dict(scope), history=history or None)
            record = self._save(question, ans, extra={"scope": scope or {}, "follow_up": follow_up if history else None})
            log.info("回答完成：首字 %s ms、共 %s ms、输入 %s / 输出 %s tokens、有效出处 %s、无效编号 %s%s",
                     ans.ttft_ms, ans.latency_ms, ans.input_tokens, ans.output_tokens, record["cited"] or "无",
                     ans.invalid_refs or "无", "，判定为没找到" if record["not_found"] else "")
            if record["not_found"]:
                log.warning("没找到答案：%s → %s", question, ans.text[:80])
            return record
        return self._start("q", work)

    def summarize(self, path: str) -> dict:
        """总结整份文件（分段提炼 + 汇总）。"""
        path = str(path or "")
        if not self._call(lambda: self._app.repo.get_file(path)):
            return {"error": "这个文件还没有索引"}
        question = f"总结：《{Path(path).name}》"

        def work(cb, t0):
            log.info("开始总结：%s", path)
            ans = self._app.summarizer.summarize_file(path, **_job_cb(cb))
            ans.latency_ms = int((time.perf_counter() - t0) * 1000)
            record = self._save(question, ans, kind="summary", extra={"path": path})
            log.info("总结完成：共 %.0f 秒，出处 %s", ans.latency_ms / 1000, record["cited"] or "无")
            return record
        out = self._start("s", work)
        return {**out, "question": question} if "qid" in out else out

    def analyze(self, question: str, scope: dict | None = None) -> dict:
        """深度分析：拆成几个方面分别检索，再按方面分析、综合结论。"""
        question = (question or "").strip()
        if not question:
            return {"error": "请输入要分析的问题"}

        def work(cb, t0):
            log.info("深度分析：%s", question)
            ans, aspects, info = self._app.analyzer.analyze(question, scope=Scope.from_dict(scope), **_job_cb(cb))
            log.info("拆分的方面：%s", "；".join(aspects))
            log.info("各语言关键词：%s；命中 %s", info["keywords"] or "（模型没给出）", info["lang_hits"])
            ans.latency_ms = int((time.perf_counter() - t0) * 1000)
            return self._save(f"分析：{question}", ans, kind="analysis",
                              extra={"aspects": aspects, "scope": scope or {}, "query": question, **info})
        return self._start("a", work)

    def extract(self, request: str, path: str | None = None, scope: dict | None = None) -> dict:
        """信息提取：按字段整理成表格（整个资料库或单个文件），每行带出处。"""
        request = (request or "").strip()
        if not request:
            return {"error": "请写清楚要提取什么，例如“列出各项目的名称、预算和负责人”"}

        def work(cb, t0):
            log.info("信息提取：%s%s", request, f"（文件 {path}）" if path else "")
            ctx_cb = cb["on_context"]
            res = self._app.extractor.extract(request, path=path or None, on_progress=lambda d, t: cb["on_progress"]("extract", d, t),
                                              on_context=ctx_cb, cancelled=cb["cancelled"],
                                              scope=None if path else Scope.from_dict(scope))
            res.latency_ms = int((time.perf_counter() - t0) * 1000)
            log.info("提取完成：%d 列 %d 行，%d 批，共 %.0f 秒", len(res.columns), len(res.rows), res.batches,
                     res.latency_ms / 1000)
            ans = Answer(text=to_markdown(res.columns, res.rows), context=res.context, no_context=not res.context,
                         latency_ms=res.latency_ms)
            cited = sorted({n for r in res.rows for n in r["refs"]})
            label = f"提取：{request}" + (f"（《{Path(path).name}》）" if path else "")
            return self._save(label, ans, kind="extract", cited=cited,
                              extra={"columns": res.columns, "rows": res.rows, "path": path, "request": request,
                                     "scope": scope or {}})
        return self._start("e", work)

    def export_csv(self, history_id: int, path: str | None = None) -> dict:
        """把提取结果存成 CSV（带 BOM，Excel 直接打开）。不给 path 时弹出“存储为”对话框。"""
        rec = self.open_history(int(history_id))
        if not rec or rec.get("kind") != "extract":
            return {"error": "这条记录不是信息提取的结果"}
        if path is None:
            if self._window is None:
                return {"error": "窗口还没准备好"}
            try:
                import webview
                kind = webview.FileDialog.SAVE if hasattr(webview, "FileDialog") else webview.SAVE_DIALOG
            except ImportError:
                kind = "save"
            stem = re.sub(r"[\\/:*?\"<>|\s]+", "_", rec["extra"].get("request", "提取结果"))[:40] or "提取结果"
            picked = self._window.create_file_dialog(kind, save_filename=f"{stem}.csv")
            if not picked:
                return {"cancelled": True}
            path = picked[0] if isinstance(picked, (list, tuple)) else picked
        text = to_csv(rec["extra"]["columns"], rec["extra"]["rows"], rec["context"])
        Path(path).write_text(text, encoding="utf-8")
        log.info("已导出 CSV：%s", path)
        return {"ok": True, "path": str(path)}

    def _save(self, question: str, ans: Answer, kind: str = "qa", extra: dict | None = None,
              cited: list[int] | None = None, thread_id: int | None = None, model: str | None = None) -> dict:
        if cited is None:
            cited = sorted({c.ref for c in ans.citations})
        ctx = [chunk_dict(i, c, set(cited)) for i, c in enumerate(ans.context, start=1)]
        # 只有“没有标注任何出处、且整段回答就是一句没找到”才算没找到；
        # 回答了一部分、另一部分说“资料中没有提到”的，照常显示回答
        if kind == "extract":
            not_found = not (extra or {}).get("rows")
        elif kind in ("chat", "search"):
            not_found = False
        else:
            not_found = bool(ans.no_context) or (not cited and is_not_found(ans.text) and len(ans.text) <= 60)
        model = model or self._app.cfg.tasks["knowledge_qa"].default_model
        hid = self._app.repo.add_history(question, ans.text, not_found, json.dumps(ctx, ensure_ascii=False),
                                         json.dumps(cited), model, ans.ttft_ms, ans.latency_ms, kind=kind,
                                         extra_json=json.dumps(extra or {}, ensure_ascii=False),
                                         thread_id=int(thread_id) if thread_id else None)
        return self._record(self._app.repo.get_history(hid))

    def _record(self, row: dict | None) -> dict | None:
        if row is None:
            return None
        from lkos.models_config import display_name, is_online_ref, load_models

        return {"id": row["id"], "thread_id": row.get("thread_id") or row["id"], "branch_of": row.get("branch_of"),
                "model_name": display_name(row["model"], load_models(self._app.home)),
                "online": is_online_ref(row["model"]), "question": row["question"], "answer": row["answer"],
                "not_found": bool(row["not_found"]), "context": json.loads(row["context_json"]),
                "cited": json.loads(row["cited_json"]), "model": row["model"], "ttft_ms": row["ttft_ms"],
                "latency_ms": row["latency_ms"], "asked_at": row["asked_at"], "kind": row.get("kind") or "qa",
                "extra": json.loads(row.get("extra_json") or "{}")}

    def stop(self) -> dict:
        self._cancel.set()
        return {"ok": True}

    def search(self, query: str, scope: dict | None = None) -> dict:
        query = (query or "").strip()
        if not query:
            return {"results": []}
        t0 = time.perf_counter()
        sc = Scope.from_dict(scope)
        extra = {} if sc.empty else {"scope": sc}
        hits = self._call(lambda: self._app.retriever.search(query, k=10, **extra))
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
                hint = next((v for k, v in PROBLEM_HINTS.items() if err.startswith(k) and v), None)
                if hint is None and err.startswith("unsupported: "):
                    hint = err.removeprefix("unsupported: ")
                problems.append({"path": f.path, "file": Path(f.path).name, "status": f.status,
                                 "hint": hint or (f"无法读取：{err}" if f.status == "failed" else err)})
            s = self._app.repo.stats()
            return {"sources": self._sources(), "problems": problems, "notes_available": apple_notes.available(),
                    "stats": {"sources": s["sources"], "files": s["files_by_status"].get("indexed", 0),
                              "chunks": s["chunks_total"]},
                    "index": self._indexer.state, "paused": self._indexer._paused.is_set(),
                    "ocr_available": ocr_available(), "tags": self._app.repo.all_tags(60)}
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
            return {"error": "这个文件夹里没有可以索引的文件（PDF、Word、PowerPoint .pptx、Markdown）"}
        self._call(lambda: self._app.repo.upsert_source(str(folder)))
        self._indexer.enqueue(str(folder))
        self._emit({"type": "library_changed"})
        return {"ok": True, "folder": str(folder), "files": n}

    def _sources(self) -> list[dict]:
        """资料库里的来源；Mac 备忘录的导出文件夹标为 kind=apple_notes（界面上显示为“Mac 备忘录”）。"""
        notes_dir = str(apple_notes.export_dir(self._app.home).resolve())
        return [{**x, "kind": "apple_notes" if x["path"] == notes_dir else "folder"} for x in self._app.repo.list_sources()]

    def add_notes(self) -> dict:
        """添加 Mac 备忘录：导出到数据目录下的 apple-notes 文件夹，按普通文件夹索引（之后按周期自动同步）。"""
        if not apple_notes.available():
            return {"error": "只有 macOS 上才能读取备忘录"}
        try:
            stats = self._call(lambda: apple_notes.sync(self._app.home, self._app.indexer.notes_fetch))
        except LkosError as e:
            return {"error": str(e)}
        folder = str(apple_notes.export_dir(self._app.home).resolve())
        def add():
            sid = self._app.repo.upsert_source(folder)
            self._app.repo.update_source_settings(sid, refresh="daily")   # 备忘录改得勤：默认每天同步
        self._call(add)
        self._indexer.enqueue(folder)
        self._emit({"type": "library_changed"})
        log.info("已添加 Mac 备忘录：%d 条（跳过加锁或已删除的 %d 条）", stats["total"], stats["skipped"])
        return {"ok": True, "folder": folder, **stats}

    def source_settings(self, source_id: int) -> dict:
        s = next((x for x in self._call(self._app.repo.list_sources) if x["id"] == int(source_id)), None)
        if s is None:
            return {"error": "没有这个文件夹"}
        return {"id": s["id"], "path": s["path"], "refresh": s["refresh"], "exclude": s["exclude"], "ocr": s["ocr"],
                "allow_online": s["allow_online"], "ocr_available": ocr_available()}

    def set_source_settings(self, source_id: int, settings: dict) -> dict:
        """settings：{"refresh", "exclude": {"patterns", "types"}, "ocr"}，只改给出的项。
        排除规则或文字识别有变化时自动重新索引这个文件夹（被排除的文件会从索引里去掉）。"""
        before = self.source_settings(source_id)
        if "error" in before:
            return before
        settings = settings or {}
        try:
            self._call(lambda: self._app.repo.update_source_settings(
                int(source_id), refresh=settings.get("refresh"), exclude=settings.get("exclude"),
                ocr=settings.get("ocr"), allow_online=settings.get("allow_online")))
        except ValueError as e:
            return {"error": str(e)}
        after = self.source_settings(source_id)
        reindex = after["exclude"] != before["exclude"] or after["ocr"] != before["ocr"]
        log.info("文件夹设置已保存：%s：更新周期 %s，排除 %s，文字识别 %s%s", after["path"], after["refresh"],
                 after["exclude"], "开" if after["ocr"] else "关", "，重新索引" if reindex else "")
        if reindex and Path(after["path"]).is_dir():
            self._indexer.enqueue(after["path"])
        self._emit({"type": "library_changed"})
        return {**after, "reindex": reindex}

    # ---------- 标签 ----------
    def tags(self, limit: int = 200) -> list[dict]:
        return self._call(lambda: self._app.repo.all_tags(int(limit)))

    def file_tags(self, path: str) -> list[dict]:
        return self._call(lambda: self._app.repo.tags_of(str(path)))

    def add_tag(self, path: str, tag: str) -> dict:
        from lkos.qa.tags import normalize_tag

        t = normalize_tag(str(tag or ""))
        if not t:
            return {"error": "标签要 1–16 个字"}
        f = self._call(lambda: self._app.repo.get_file(str(path)))
        if f is None:
            return {"error": "这个文件还没有索引"}
        self._call(lambda: self._app.repo.add_tag(f.id, t, "user"))
        log.info("添加标签：%s → %s", Path(path).name, t)
        return {"ok": True, "tags": self.file_tags(path)}

    def remove_tag(self, path: str, tag: str) -> dict:
        f = self._call(lambda: self._app.repo.get_file(str(path)))
        if f is None:
            return {"error": "这个文件还没有索引"}
        self._call(lambda: self._app.repo.remove_tag(f.id, str(tag)))
        log.info("删除标签：%s → %s", Path(path).name, tag)
        return {"ok": True, "tags": self.file_tags(path)}

    def generate_tags(self, folder: str) -> dict:
        """立即给这个文件夹里还没有标签的文件生成标签（不等电脑空闲）。"""
        return {"ok": self._indexer.enqueue(str(folder), background=False, kind="tags")}

    # ---------- 后台服务（关掉窗口后继续索引） ----------
    def daemon_status(self) -> dict:
        return daemon_mod.status(self._app.home)

    def set_daemon(self, enabled: bool) -> dict:
        try:
            if enabled:
                daemon_mod.install(self._app.home)
            else:
                daemon_mod.uninstall()
        except LkosError as e:
            return {"error": str(e)}
        return daemon_mod.status(self._app.home)

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

    # ---------- 设置与日志（开发者模式） ----------
    def _settings_path(self) -> Path:
        return self._app.home / SETTINGS_FILE

    def _load_settings(self) -> dict:
        try:
            data = json.loads(self._settings_path().read_text("utf-8"))
            return {**DEFAULT_SETTINGS, **{k: v for k, v in data.items() if k in DEFAULT_SETTINGS}}
        except (OSError, ValueError):
            return dict(DEFAULT_SETTINGS)

    def settings(self) -> dict:
        return dict(self._settings)

    def set_setting(self, key: str, value) -> dict:
        if key not in DEFAULT_SETTINGS:
            return {"error": f"没有这个设置：{key}"}
        self._settings[key] = type(DEFAULT_SETTINGS[key])(value)
        try:
            self._settings_path().write_text(json.dumps(self._settings, ensure_ascii=False, indent=2), "utf-8")
        except OSError as e:
            log.warning("保存设置失败：%s", e)
        if key == "auto_tags" and value:
            for src in self._call(self._app.repo.list_sources):
                self._indexer.enqueue(src["path"], background=True, kind="tags")
        if key == "dev_mode":
            set_level(bool(value))
            log.info("开发者模式已%s（日志级别：%s）", "开启" if value else "关闭", "调试" if value else "信息")
        return dict(self._settings)

    def logs(self, after: int = 0, level: str = "DEBUG") -> dict:
        """增量拉取内存日志：after 为上次拿到的最大序号。"""
        lines = self._ring.since(int(after or 0), LEVELS.get(str(level).upper(), 10))
        return {"lines": lines, "last": self._ring.seq, "file": str(self._app.home / "logs" / "lkos.log")}

    def calls(self, limit: int = 100) -> list[dict]:
        """最近的模型调用（来自 call_logs 表）：首字、总耗时、token、内存、错误。"""
        rows = self._call(lambda: self._app.repo.recent_calls(limit=int(limit)))
        keep = ("ts", "task", "model", "ttft_ms", "latency_ms", "input_tokens", "output_tokens",
                "mem_available_mb", "swap_used_mb", "error")
        return [{k: r.get(k) for k in keep} for r in rows]

    def open_logs_folder(self) -> dict:
        folder = self._app.home / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        problem = self._opener(str(folder))
        return {"error": problem} if problem else {"ok": True}

    # ---------- 打开文件 ----------
    def open_file(self, path: str) -> dict:
        notes_root = str(apple_notes.export_dir(self._app.home).resolve()) + os.sep
        nid = apple_notes.note_id_for(self._app.home, str(path)) if str(path).startswith(notes_root) else None
        if nid:                                   # 备忘录：在备忘录应用里显示这一条
            problem = apple_notes.show_note(nid)
            return {"error": problem} if problem else {"ok": True}
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
