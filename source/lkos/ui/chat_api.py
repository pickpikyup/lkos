"""对话的桥接方法（混入 Bridge）：三种模式（关键词检索 / 分析 / 深度分析）、对话串、分支、在线发送规则。

在线发送规则：
1. 默认只发本轮检索到的片段和对话记录，不发整份文件；
2. 发送前列出要发的片段，用户可以去掉不想发的（设置里可以关掉这一步）；
3. 用户主动附加的文件（📎 / @）发送解析后的全文；本机模型则改为只在这些文件里检索；
4. 文件夹设置里关掉“允许发送到在线模型”的，内容永远不发；
5. 每次发送都写日志（服务、片段数、文件）。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from lkos.errors import Cancelled, LkosError
from lkos.logs import get_logger
from lkos.qa.history import turns_from_records
from lkos.retrieval.keyword import keyword_search, result_dict
from lkos.types import Scope

log = get_logger("ui")
ATTACH_MAX = 10
SEARCH_SAVE_FILES = 50     # 关键词检索结果存进对话的最多文件数
SEARCH_SAVE_HITS = 10


class ChatMixin:
    # 由 Bridge 提供：self._app、self._call、self._emit、self._start、self._save、self._record、self._cancel、
    # self._settings、self.model_choices

    # ---------- 发送 ----------
    def send(self, question: str, mode: str = "qa", thread_id: int | None = None, model: str | None = None,
             scope: dict | None = None, attachments: list[str] | None = None) -> dict:
        """mode：search（关键词检索，不用模型）/ qa（分析）/ analysis（深度分析）。
        thread_id：接着哪个对话问（空 = 新对话）；model：模型引用（空 = 本机默认模型）；
        attachments：附加的文件路径。"""
        question = (question or "").strip()
        if not question:
            return {"error": "请输入内容"}
        if mode == "search":
            return self._keyword(question, thread_id, scope)
        if mode not in ("qa", "analysis"):
            return {"error": f"不支持的模式：{mode}"}
        choices = {c["ref"]: c for c in self.model_choices()}
        ref = model or next(iter(choices))
        choice = choices.get(ref)
        if choice is None:
            return {"error": "这个模型现在不可用（可能已删除、停用，或开着离线模式），请重新选择"}
        online = choice["online"]
        if online and not choice.get("consented"):
            return {"error": "第一次使用这个在线模型，需要先确认", "need_consent":
                    {"id": choice["id"], "name": choice["name"], "service": choice["service"]}}
        attachments = [str(p) for p in attachments or []][:ATTACH_MAX]

        def work(cb, t0):
            qid = cb["qid"]
            turns = turns_from_records(self._app.repo.thread_turns(int(thread_id))) if thread_id else []
            sc = Scope.from_dict(scope)
            pinned = []
            if attachments and online:
                pinned = self._attachment_chunks(attachments)
            elif attachments:
                sc.paths = attachments      # 本机模型放不下整份文件：只在附加的文件里检索
            sent: dict = {}
            confirm = self._online_gate(qid, choice, turns, sent) if online else None
            label = question if mode == "qa" else f"分析：{question}"
            log.info("%s：%s（模型 %s%s%s）", "提问" if mode == "qa" else "深度分析", question, choice["name"],
                     f"，接着对话 #{thread_id} 第 {len(turns) + 1} 轮" if turns else "",
                     f"，附加 {len(attachments)} 个文件" if attachments else "")
            common = dict(model_override=ref, on_token=cb["on_token"], on_context=cb["on_context"], scope=sc,
                          history=turns, pinned=pinned, confirm=confirm)
            extra = {"mode": mode, "scope": scope or {}, "attachments": attachments}
            if mode == "qa":
                ans = self._app.qa.ask(question, **common)
                kind = "qa"
            else:
                ans, aspects, info = self._app.analyzer.analyze(question, on_progress=cb["on_progress"],
                                                                cancelled=cb["cancelled"], **common)
                extra.update(aspects=aspects, query=question, **info)
                kind = "analysis"
            ans.latency_ms = int((time.perf_counter() - t0) * 1000)
            if online:
                extra["sent"] = sent
            record = self._save(label, ans, kind=kind, extra=extra, thread_id=thread_id, model=ref)
            log.info("回答完成（%s）：首字 %s ms、共 %s ms、输入 %s / 输出 %s tokens、出处 %s", choice["name"], ans.ttft_ms,
                     ans.latency_ms, ans.input_tokens, ans.output_tokens, record["cited"] or "无")
            return record
        return self._start("q" if mode == "qa" else "a", work)

    def _keyword(self, query: str, thread_id, scope) -> dict:
        def run():
            res = keyword_search(self._app.repo, query, Scope.from_dict(scope))
            d = result_dict(res)
            d["files"] = [{**f, "hits": f["hits"][:SEARCH_SAVE_HITS]} for f in d["files"][:SEARCH_SAVE_FILES]]
            d["file_count"] = len(res.files)
            summary = (f"找到 {res.total_hits} 处，在 {len(res.files)} 个文件里" if res.files
                       else "没有找到包含这些词的文件")
            from lkos.types import Answer

            hid = self._save(query, Answer(text=summary, latency_ms=res.ms), kind="search",
                             extra={"mode": "search", "results": d, "scope": scope or {}}, thread_id=thread_id,
                             model="-/关键词检索")
            log.info("关键词检索：%s → %d 处、%d 个文件（%d ms）", " + ".join(res.terms), res.total_hits,
                     len(res.files), res.ms)
            return hid
        return {"record": self._call(run)}

    # ---------- 在线发送 ----------
    def _allowed(self, path: str, blocked: list[str]) -> bool:
        return not any(path == b or path.startswith(b.rstrip("/") + "/") for b in blocked)

    def _attachment_chunks(self, paths: list[str]):
        blocked = self._app.repo.offline_only_folders()
        out = []
        for p in paths:
            if not self._allowed(p, blocked):
                raise LkosError(f"《{Path(p).name}》所在的文件夹设置为不允许发送到在线模型")
            out += self._app.repo.chunks_of_file(p)
        return out

    def _online_gate(self, qid: str, choice: dict, turns, sent: dict):
        """发往在线模型前：去掉不允许发送的文件夹里的片段；需要时等用户在界面上确认。"""
        def confirm(ctx):
            blocked = self._app.repo.offline_only_folders()
            keep = [c for c in ctx if self._allowed(c.file_path, blocked)]
            excluded = len(ctx) - len(keep)
            if self._settings.get("confirm_online", True):
                ev = threading.Event()
                self._pending[qid] = {"event": ev, "keep": None}
                from lkos.ui.bridge import chunk_dict

                self._emit({"type": "confirm_send", "qid": qid, "model": choice["name"], "service": choice["service"],
                            "items": [chunk_dict(i, c) for i, c in enumerate(keep, start=1)], "excluded": excluded,
                            "turns": len(turns)})
                while not ev.wait(0.2):
                    if self._cancel.is_set():
                        self._pending.pop(qid, None)
                        raise Cancelled("已取消发送")
                picked = self._pending.pop(qid)["keep"]
                if picked is None:
                    raise Cancelled("已取消发送")
                keep = [c for i, c in enumerate(keep, start=1) if i in set(picked)]
            files = sorted({c.file_name for c in keep})
            sent.update(service=choice["service"], model=choice["name"], chunks=len(keep), files=files,
                        excluded=excluded, turns=len(turns))
            log.info("发送到在线模型 %s（%s）：%d 段资料、%d 轮对话%s；文件：%s", choice["name"], choice["service"],
                     len(keep), len(turns), f"，按文件夹设置排除 {excluded} 段" if excluded else "",
                     "、".join(files) or "无")
            return keep
        return confirm

    def confirm_send(self, qid: str, keep: list[int] | None) -> dict:
        """界面上点了“发送”（keep = 保留的片段编号）或“取消”（keep = None）。"""
        p = self._pending.get(qid)
        if p is None:
            return {"error": "这次发送已经结束"}
        p["keep"] = [int(x) for x in keep] if keep is not None else None
        p["event"].set()
        return {"ok": True}

    # ---------- 附加文件 ----------
    def find_files(self, query: str, limit: int = 20) -> list[dict]:
        """@ / 📎：按文件名找已索引的文件。"""
        q = (query or "").strip()

        def run():
            sql = ("SELECT f.path, f.file_type, f.chunk_count, (SELECT SUM(length(text)) FROM chunks c "
                   "WHERE c.file_id = f.id) AS chars FROM files f WHERE f.status = 'indexed'")
            args: list = []
            for part in q.split():
                sql += " AND instr(lower(f.path), lower(?)) > 0"
                args.append(part)
            sql += " ORDER BY f.mtime DESC LIMIT ?"
            args.append(int(limit))
            blocked = self._app.repo.offline_only_folders()
            return [{"path": r["path"], "name": Path(r["path"]).name, "folder": str(Path(r["path"]).parent),
                     "type": r["file_type"], "chars": int(r["chars"] or 0), "tokens": int((r["chars"] or 0) * 0.7),
                     "online_ok": self._allowed(r["path"], blocked)}
                    for r in self._app.repo.conn.execute(sql, args)]
        return self._call(run)

    # ---------- 对话串 ----------
    def thread(self, thread_id: int) -> list[dict]:
        return [self._record(r) for r in self._call(lambda: self._app.repo.thread_turns(int(thread_id)))]

    def branch(self, turn_id: int) -> dict:
        """从这一轮分支：保留到这一轮为止的内容，另开一个对话。"""
        try:
            tid = self._call(lambda: self._app.repo.branch_thread(int(turn_id)))
        except ValueError as e:
            return {"error": str(e)}
        log.info("从第 #%s 轮分支出新对话 #%s", turn_id, tid)
        return {"thread_id": tid, "turns": self.thread(tid)}

    def delete_thread(self, thread_id: int) -> dict:
        n = self._call(lambda: self._app.repo.delete_thread(int(thread_id)))
        return {"ok": True, "deleted": n}
