"""交互式命令行应用：直接输入问题即可问答，斜杠命令管理文件夹、搜索、打开原文件。

启动：在终端运行 source/bin/lkos（不带参数）。
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from rich.console import Console

from lkos.app import App
from lkos.errors import LkosError
from lkos.types import Answer, RetrievedChunk

HELP = """[bold]直接输入问题[/]并回车即可提问，回答附带出处编号。

[bold]命令[/]
  /s 关键词          只搜索，不生成回答（快）
  /open 编号         打开上一次回答或搜索里第 N 条出处的原文件
  /ctx               显示上一次送给模型的原文片段
  /add 文件夹        添加要检索的文件夹并立即建索引（可以把文件夹拖进终端窗口）
  /update            更新所有文件夹的索引（只处理新增、修改、删除的文件）
  /folders           列出已添加的文件夹
  /remove 编号       移除文件夹及其索引（不会删除你的原文件）
  /status            索引规模与模型状态
  /help              显示本帮助
  /quit              退出（也可以按 Ctrl-D）

建索引时按 Ctrl-C 可以随时停下，已完成的部分会保存，下次 /update 接着做。"""


def clean_path(raw: str) -> Path:
    """接受拖进终端的路径：去掉引号、反斜杠转义，展开 ~。"""
    raw = raw.strip()
    try:
        parts = shlex.split(raw)
        raw = parts[0] if len(parts) == 1 else raw
    except ValueError:
        raw = raw.strip("'\"")
    return Path(raw).expanduser()


def open_file(path: str) -> str | None:
    """用系统默认程序打开文件；返回错误信息或 None。"""
    if not Path(path).exists():
        return f"文件已不存在：{path}"
    cmd = ["open", path] if sys.platform == "darwin" else ["xdg-open", path]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return f"无法自动打开，请手动打开：{path}"
    return None


class Shell:
    def __init__(self, a: App, console: Console, err: Console):
        self.a = a
        self.console = console
        self.err = err
        self.refs: list[RetrievedChunk] = []   # /open 用：上一次回答的出处或搜索结果
        self.context: list[RetrievedChunk] = []

    # ---------- 启动 ----------
    def banner(self) -> None:
        s = self.a.repo.stats()
        self.console.print("[bold]Local Knowledge OS[/]　本地知识库问答（离线运行）")
        self.console.print(f"[dim]{s['sources']} 个文件夹 · {s['files_by_status'].get('indexed', 0)} 个文件 · "
                           f"{s['chunks_total']} 个文本块[/]")
        missing = [ref for ref, ok, _ in self.a.gateway.check() if not ok]
        if missing:
            self.console.print(f"[yellow]模型未就绪：{'、'.join(missing)}。请先打开 Ollama 应用；"
                               f"缺模型时运行 ollama pull <模型名>。[/]")
        if not s["sources"]:
            self.console.print("还没有添加文件夹。输入 [bold]/add 文件夹路径[/] 开始（可以把文件夹拖进来）。")
        self.console.print("[dim]直接输入问题提问；/help 查看命令。[/]")

    # ---------- 主循环 ----------
    def run(self) -> None:
        _enable_history(self.a.home / ".history")
        self.banner()
        while True:
            try:
                line = input("\n问> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                break
            if not line:
                continue
            try:
                if not self.handle(line):
                    break
            except KeyboardInterrupt:
                self.console.print("\n[yellow]已中断。[/]")
            except LkosError as e:
                self.err.print(f"[bold red]错误：[/]{e}")

    def handle(self, line: str) -> bool:
        """处理一行输入；返回 False 表示退出。"""
        if not line.startswith("/"):
            self.ask(line)
            return True
        cmd, _, arg = line[1:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("quit", "exit", "q"):
            return False
        actions = {
            "help": lambda: self.console.print(HELP), "h": lambda: self.console.print(HELP),
            "s": lambda: self.search(arg), "search": lambda: self.search(arg),
            "open": lambda: self.open(arg), "o": lambda: self.open(arg),
            "ctx": self.show_context,
            "add": lambda: self.add(arg),
            "update": self.update,
            "folders": self.folders, "sources": self.folders,
            "remove": lambda: self.remove(arg),
            "status": self.status,
        }
        action = actions.get(cmd)
        if action is None:
            self.console.print(f"没有这个命令：/{cmd}。输入 /help 查看命令。")
        else:
            action()
        return True

    # ---------- 问答与搜索 ----------
    def ask(self, question: str) -> None:
        if not self.a.repo.stats()["chunks_total"]:
            self.console.print("索引还是空的。先用 /add 添加文件夹。")
            return
        started = []

        def on_token(t: str) -> None:
            if not started:
                started.append(True)
            sys.stdout.write(t)
            sys.stdout.flush()

        with self.console.status("检索并思考中…") as status:
            def first_token(t: str) -> None:
                if not started:
                    status.stop()
                on_token(t)

            ans = self.a.qa.ask(question, on_token=first_token)
        if not started:  # 没有流式输出（没找到资料）
            self.console.print(ans.text, highlight=False)
        else:
            sys.stdout.write("\n")
        self.show_answer_footer(ans)

    def show_answer_footer(self, ans: Answer) -> None:
        self.context = list(ans.context)
        if ans.no_context:
            self.refs = []
            return
        self.refs = [c.chunk for c in ans.citations]
        if ans.citations:
            self.console.print("\n[bold]出处[/]")
            for i, c in enumerate(ans.citations, start=1):
                self.console.print(f"  [{i}] {c.chunk.describe()}", highlight=False)
            self.console.print("[dim]/open 编号 打开原文件 · /ctx 查看原文片段[/]")
        else:
            self.console.print("\n[yellow]回答没有标注出处，请用 /ctx 核对原文。[/]")
            self.refs = list(ans.context)
        parts = []
        if ans.ttft_ms is not None:
            parts.append(f"首字 {ans.ttft_ms / 1000:.1f}s")
        if ans.latency_ms is not None:
            parts.append(f"共 {ans.latency_ms / 1000:.1f}s")
        if parts:
            self.console.print("[dim]" + " · ".join(parts) + "[/]")

    def search(self, query: str) -> None:
        if not query:
            self.console.print("用法：/s 关键词")
            return
        hits = self.a.retriever.search(query, k=8)
        self.refs, self.context = hits, hits
        if not hits:
            self.console.print("没有找到相关内容。")
            return
        for i, h in enumerate(hits, start=1):
            snippet = " ".join(h.text.split())
            self.console.print(f"[bold][{i}][/] {h.describe()}", highlight=False)
            self.console.print(f"    [dim]{snippet[:140]}{'…' if len(snippet) > 140 else ''}[/]", highlight=False)
        self.console.print("[dim]/open 编号 打开原文件[/]")

    def open(self, arg: str) -> None:
        if not self.refs:
            self.console.print("先提问或搜索，再用 /open 编号 打开出处。")
            return
        try:
            n = int(arg.strip("[] "))
            if n < 1:
                raise IndexError
            ref = self.refs[n - 1]
        except (ValueError, IndexError):
            self.console.print(f"请输入 1–{len(self.refs)} 之间的编号，例如 /open 1")
            return
        problem = open_file(ref.file_path)
        loc = ref.location.describe(ref.file_type)
        self.console.print(problem or f"已打开：{ref.file_name}" + (f"（{loc}）" if loc else ""), highlight=False)

    def show_context(self) -> None:
        if not self.context:
            self.console.print("还没有可以显示的片段。")
            return
        for i, c in enumerate(self.context, start=1):
            self.console.print(f"\n[bold][{i}] {c.describe()}[/]", highlight=False)
            self.console.print(c.text, highlight=False)

    # ---------- 文件夹与索引 ----------
    def _index(self, folder: Path) -> None:
        from lkos.cli import print_index_report, run_index

        self.console.print(f"正在索引：{folder}（Ctrl-C 可随时停下，下次 /update 接着做）")
        try:
            rep, _ = run_index(self.a, folder)
        except KeyboardInterrupt:
            self.console.print("\n[yellow]已停下。已完成的文件已保存，之后输入 /update 继续。[/]")
            return
        print_index_report(rep, brief=True)

    def add(self, arg: str) -> None:
        if not arg:
            self.console.print("用法：/add 文件夹路径（可以把文件夹拖进终端窗口）")
            return
        folder = clean_path(arg)
        if not folder.is_dir():
            self.console.print(f"找不到这个文件夹：{folder}")
            return
        folder = folder.resolve()
        n = sum(1 for _ in self.a.indexer.walk(folder))
        self.console.print(f"找到 {n} 个可索引的文件（PDF / Word / PowerPoint / Markdown）。")
        if n == 0:
            return
        self._index(folder)

    def update(self) -> None:
        sources = self.a.repo.list_sources()
        if not sources:
            self.console.print("还没有添加文件夹。用 /add 文件夹路径 添加。")
            return
        for s in sources:
            folder = Path(s["path"])
            if not folder.is_dir():
                self.console.print(f"[yellow]跳过（文件夹不存在或未连接）：{folder}[/]")
                continue
            self._index(folder)

    def folders(self) -> None:
        sources = self.a.repo.list_sources()
        if not sources:
            self.console.print("还没有添加文件夹。用 /add 文件夹路径 添加。")
            return
        for i, s in enumerate(sources, start=1):
            gone = "" if Path(s["path"]).is_dir() else "  [yellow]（找不到）[/]"
            failed = f" · 失败 {s['failed']}" if s["failed"] else ""
            last = (s["last_indexed"] or "从未完成")[:16].replace("T", " ")
            self.console.print(f"[bold][{i}][/] {s['path']}{gone}", highlight=False)
            self.console.print(f"    [dim]{s['files']} 个文件 · {s['chunks']} 个文本块{failed} · 最近更新 {last}（UTC）[/]")

    def remove(self, arg: str) -> None:
        sources = self.a.repo.list_sources()
        try:
            if int(arg) < 1:
                raise IndexError
            s = sources[int(arg) - 1]
        except (ValueError, IndexError):
            self.console.print("用法：/remove 编号（编号见 /folders）")
            return
        answer = input(f"移除 {s['path']} 的索引（原文件不受影响）？输入 y 确认：").strip().lower()
        if answer != "y":
            self.console.print("已取消。")
            return
        n = self.a.repo.remove_source(s["id"])
        self.refs, self.context = [], []
        self.console.print(f"已移除，删除了 {n} 个文件的索引。")

    def status(self) -> None:
        s = self.a.repo.stats()
        size_mb = self.a.db_path.stat().st_size / 2**20 if self.a.db_path.exists() else 0
        self.console.print(f"文件夹 {s['sources']} 个 · 已索引文件 {s['files_by_status'].get('indexed', 0)} 个 · "
                           f"文本块 {s['chunks_total']} 个 · 数据库 {size_mb:.0f} MB")
        bad = s["files_by_status"].get("failed", 0) + s["files_by_status"].get("skipped", 0)
        if bad:
            self.console.print(f"[yellow]{bad} 个文件没能索引（加密、扫描件或格式问题），"
                               f"终端里运行 source/bin/lkos status --failed 查看，或在应用的资料库页查看。[/]")
        for ref, ok, msg in self.a.gateway.check():
            self.console.print(f"{'[green]就绪[/]' if ok else '[red]不可用[/]'} {ref}  [dim]{msg}[/]", highlight=False)


def _enable_history(path: Path) -> None:
    """上下方向键翻历史问题（readline 不可用时忽略）。"""
    try:
        import atexit
        import readline
    except ImportError:
        return
    try:
        readline.read_history_file(str(path))
    except OSError:
        pass
    readline.set_history_length(500)
    atexit.register(lambda: _save_history(readline, path))


def _save_history(readline, path: Path) -> None:
    try:
        readline.write_history_file(str(path))
    except OSError:
        pass
