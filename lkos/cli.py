"""命令行入口：只负责解析参数和显示结果，业务逻辑都在服务层。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from lkos.app import CONFIG_NAME, App
from lkos.config import default_home
from lkos.errors import LkosError
from lkos.types import Answer, ProgressEvent

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Local Knowledge OS（POC）：本地知识库索引、检索与问答。")
console = Console()
err = Console(stderr=True)


class State:
    home: Optional[Path] = None
    config: Optional[Path] = None
    app: Optional[App] = None


state = State()


@app.callback()
def _global(
    home: Optional[Path] = typer.Option(None, "--home", envvar="LKOS_HOME",
                                        help="数据目录（数据库、配置、缓存）。默认为项目目录下的 data/。"),
    config: Optional[Path] = typer.Option(None, "--config", help="配置文件路径，默认 <home>/config.yaml。"),
):
    global console, err
    console, err = Console(), Console(stderr=True)  # 每次调用重新读取终端宽度等环境
    state.home = home
    state.config = config


def open_app() -> App:
    state.app = App.open(home=state.home, config_path=state.config)
    return state.app


def fail(e: Exception) -> None:
    err.print(f"[bold red]错误：[/]{e}")
    if state.app is not None:  # 出错退出前也关闭数据库连接
        state.app.close()
        state.app = None
    raise typer.Exit(code=1)


# ---------------- init ----------------
@app.command()
def init():
    """创建数据目录与数据库，检查 Ollama 与模型是否就绪。"""
    try:
        home = (state.home or default_home()).expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True)
        cfg_file = state.config or home / CONFIG_NAME
        if not cfg_file.exists():
            from importlib import resources

            cfg_file.write_text(resources.files("lkos").joinpath("config.default.yaml").read_text("utf-8"),
                                encoding="utf-8")
            console.print(f"已写入默认配置：{cfg_file}")
        a = open_app()
    except LkosError as e:
        fail(e)
    console.print(f"数据目录：{a.home}")
    console.print(f"数据库：{a.db_path}")
    table = Table("模型", "状态", "说明")
    all_ok = True
    for ref, ok, msg in a.gateway.check():
        all_ok &= ok
        table.add_row(ref, "[green]就绪[/]" if ok else "[red]不可用[/]", msg)
    console.print(table)
    a.close()
    if not all_ok:
        raise typer.Exit(code=2)


# ---------------- index ----------------
@app.command()
def index(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True, help="要索引的文件夹"),
    limit: Optional[int] = typer.Option(None, "--limit", min=1, help="只处理前 N 个文件（不清理已删除文件）"),
):
    """索引文件夹（增量：未变化的文件跳过，改名的文件不重新计算 Embedding）。"""
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

    try:
        a = open_app()
    except LkosError as e:
        fail(e)
    total = sum(1 for _ in a.indexer.walk(folder.resolve()))
    if limit:
        total = min(total, limit)
    labels = {"indexed": "已索引", "unchanged": "未变化", "moved": "已移动", "skipped": "已跳过", "failed": "失败"}
    try:
        with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(),
                      console=console, transient=True) as progress:
            task = progress.add_task("索引中", total=total)

            def on_progress(ev: ProgressEvent) -> None:
                progress.update(task, completed=ev.files_seen,
                                description=f"{labels.get(ev.action, ev.action)} {Path(ev.path).name[:40]}")

            rep = a.indexer.index_folder(folder, limit=limit, on_progress=on_progress)
    except KeyboardInterrupt:
        err.print("已中断。已完成的文件已保存，重新运行即可续传。")
        raise typer.Exit(code=130)
    except LkosError as e:
        fail(e)
    finally:
        a.close()

    table = Table("项目", "数量", show_header=False)
    for label, value in [("扫描文件", rep.files_seen), ("新索引", rep.files_indexed), ("未变化/跳过", rep.files_skipped),
                         ("改名/移动", rep.files_moved), ("失败", rep.files_failed), ("已删除", rep.files_removed),
                         ("新增文本块", rep.chunks_added), ("其中复用缓存向量", rep.chunks_reused),
                         ("清理无用向量", rep.vectors_pruned), ("耗时（秒）", rep.seconds),
                         ("　解析（秒）", rep.parse_seconds), ("　计算向量（秒）", rep.embed_seconds)]:
        table.add_row(label, str(value))
    console.print(table)
    if rep.files_indexed and rep.seconds:
        console.print(f"吞吐：{rep.files_indexed / rep.seconds:.2f} 文件/秒，{rep.chunks_added / rep.seconds:.1f} 文本块/秒")
    if not rep.cleanup_done:
        console.print("[yellow]使用了 --limit，本次未清理已删除的文件。[/]")
    for path, reason in rep.failures[:10]:
        err.print(f"[red]失败[/] {path}：{reason}")


# ---------------- search ----------------
@app.command()
def search(
    query: str = typer.Argument(..., help="检索内容"),
    k: Optional[int] = typer.Option(None, "--k", min=1, max=50, help="返回片段数，默认取配置 retrieval.top_k"),
    lang: str = typer.Option("auto", "--lang", help="auto | zh | en | fr | all"),
    lang_mode: Optional[str] = typer.Option(None, "--lang-mode", help="soft（默认，同语言加权）| strict（只检索同语言）| all"),
):
    """只检索不生成：显示前 k 个片段、分数和位置。"""
    try:
        a = open_app()
        hits = a.retriever.search(query, k=k, lang=lang, mode=lang_mode)
        dbg = a.retriever.last_debug
    except LkosError as e:
        fail(e)
    console.print(f"问题语言：{dbg.lang}　模式：{dbg.mode}　过滤：{dbg.lang_filter or '不过滤'}　"
                  f"关键词：{dbg.fts_query or '（无）'}")
    if not hits:
        console.print("没有找到相关片段。")
    for i, h in enumerate(hits, start=1):
        console.print(f"[bold][{i}][/] {h.describe()}  [dim]score={h.score:.4f} lang={h.lang}[/]")
        snippet = h.text.replace("\n", " ")
        console.print(f"    {snippet[:160]}{'…' if len(snippet) > 160 else ''}", highlight=False)
    a.close()


# ---------------- ask / chat ----------------
def print_answer(ans: Answer, streamed: bool, show_context: bool) -> None:
    if ans.no_context:
        console.print(ans.text)
        return
    if not streamed:
        console.print(ans.text, highlight=False)
    else:
        console.print()
    if ans.citations:
        console.print("\n[bold]引用[/]")
        for c in ans.citations:
            console.print(f"[{c.ref}] {c.chunk.describe()}", highlight=False)
    else:
        console.print("\n[yellow]回答中没有有效引用。[/]")
    if ans.invalid_refs:
        console.print(f"[yellow]已移除 {len(ans.invalid_refs)} 个无效引用：{ans.invalid_refs}[/]")
    if show_context:
        console.print("\n[bold]送入模型的片段[/]")
        for i, c in enumerate(ans.context, start=1):
            console.print(f"[{i}] {c.describe()}\n{c.text}\n", highlight=False)
    parts = []
    if ans.ttft_ms is not None:
        parts.append(f"首 token {ans.ttft_ms / 1000:.1f}s")
    if ans.latency_ms is not None:
        parts.append(f"总耗时 {ans.latency_ms / 1000:.1f}s")
    if ans.input_tokens is not None:
        parts.append(f"输入 {ans.input_tokens:,} / 输出 {ans.output_tokens or 0:,} tokens")
    from lkos.gateway.gateway import memory_snapshot

    mem, swap = memory_snapshot()
    if mem is not None:
        parts.append(f"可用内存 {mem / 1024:.1f} GB · 交换区 {(swap or 0) / 1024:.1f} GB")
    console.print("\n[dim]" + " · ".join(parts) + "[/]")


def _ask_once(a: App, question: str, k: int, model: Optional[str], lang: str, show_context: bool,
              stream: bool, mode: Optional[str] = None) -> None:
    on_token = (lambda t: (sys.stdout.write(t), sys.stdout.flush())) if stream else None
    ans = a.qa.ask(question, k=k, model_override=model, lang=lang, on_token=on_token, mode=mode)
    print_answer(ans, streamed=stream and not ans.no_context, show_context=show_context)


@app.command()
def ask(
    question: str = typer.Argument(..., help="问题"),
    k: Optional[int] = typer.Option(None, "--k", min=1, max=20, help="片段数，默认取配置 retrieval.top_k"),
    model: Optional[str] = typer.Option(None, "--model", help="本次使用的模型，如 local/qwen3:4b-instruct"),
    lang: str = typer.Option("auto", "--lang", help="auto | zh | en | fr | all"),
    lang_mode: Optional[str] = typer.Option(None, "--lang-mode", help="soft（默认，同语言加权）| strict（只检索同语言）| all"),
    show_context: bool = typer.Option(False, "--show-context", help="显示送入模型的片段"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="流式输出"),
):
    """检索 + 生成：输出带引用的回答。"""
    try:
        a = open_app()
        _ask_once(a, question, k, model, lang, show_context, stream, lang_mode)
    except LkosError as e:
        fail(e)
    a.close()


@app.command()
def chat(
    k: Optional[int] = typer.Option(None, "--k", min=1, max=20, help="片段数，默认取配置 retrieval.top_k"),
    model: Optional[str] = typer.Option(None, "--model"),
    lang: str = typer.Option("auto", "--lang"),
    lang_mode: Optional[str] = typer.Option(None, "--lang-mode", help="soft（默认，同语言加权）| strict（只检索同语言）| all"),
    show_context: bool = typer.Option(False, "--show-context"),
    stream: bool = typer.Option(True, "--stream/--no-stream"),
):
    """交互式连续提问（每轮独立检索，POC 不做多轮记忆）。输入 exit 或 Ctrl-D 退出。"""
    try:
        a = open_app()
    except LkosError as e:
        fail(e)
    console.print("输入问题后回车；exit 退出。")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit", ":q"):
            break
        try:
            _ask_once(a, q, k, model, lang, show_context, stream, lang_mode)
        except LkosError as e:
            err.print(f"[bold red]错误：[/]{e}")
    a.close()


# ---------------- status ----------------
@app.command()
def status(failed: bool = typer.Option(False, "--failed", help="列出失败与跳过的文件")):
    """显示索引规模与最近一次索引结果。"""
    try:
        a = open_app()
    except LkosError as e:
        fail(e)
    s = a.repo.stats()
    size_mb = a.db_path.stat().st_size / 2**20 if a.db_path.exists() else 0
    table = Table("项目", "值", show_header=False)
    rows = [
        ("数据目录", str(a.home)),
        ("Embedding 模型", f"{a.cfg.embedding.ref}（{a.cfg.embedding.dim} 维）"),
        ("问答模型", a.cfg.tasks["knowledge_qa"].default_model),
        ("上下文 / 片段数", f"{a.cfg.chat_model(a.cfg.tasks['knowledge_qa'].default_model).context_window} tokens / "
                           f"{a.cfg.retrieval.top_k}"),
        ("语言模式", a.cfg.retrieval.language_filter),
        ("来源文件夹", str(s["sources"])),
        ("文件", f"{s['files_total']}  {s['files_by_status']}"),
        ("已索引格式", str(s["files_by_type"])),
        ("文本块", f"{s['chunks_total']}  {s['chunks_by_lang']}"),
        ("向量（去重后）", str(s["vectors_total"])),
        ("数据库大小", f"{size_mb:.1f} MB"),
        ("模型调用次数", str(s["calls_total"])),
        ("远端调用", "开启" if a.cfg.privacy.remote_enabled and not a.cfg.privacy.offline_mode else "关闭"),
    ]
    if s["last_run"]:
        r = s["last_run"]
        rows.append(("最近一次索引", f"{r['finished_at']}  新索引 {r['files_indexed']} · 失败 {r['files_failed']}"
                                     + (" · [red]中途中断[/]" if r["aborted"] else "")))
    for row in rows:
        table.add_row(*row)
    console.print(table)
    if failed:
        bad = a.repo.failed_files()
        if not bad:
            console.print("没有失败或跳过的文件。")
        for f in bad:
            console.print(f"[{'red' if f.status == 'failed' else 'yellow'}]{f.status}[/] {f.path}：{f.error}",
                          highlight=False)
    a.close()


# ---------------- eval ----------------
@app.command("eval")
def eval_cmd(
    questions: Path = typer.Argument(..., exists=True, dir_okay=False, help="测试问题集 YAML"),
    k: Optional[int] = typer.Option(None, "--k", min=1, max=20, help="片段数，默认取配置 retrieval.top_k"),
    with_answer: bool = typer.Option(False, "--with-answer", help="同时评测生成与引用"),
    lang: str = typer.Option("auto", "--lang", help="auto（自动识别问题语言）| all（不区分）| zh | en | fr"),
    lang_mode: Optional[str] = typer.Option(None, "--lang-mode", help="soft（默认，同语言加权）| strict（只检索同语言）| all"),
    model: Optional[str] = typer.Option(None, "--model", help="本次评测使用的问答模型，如 local/qwen3:1.7b"),
    out: Optional[Path] = typer.Option(None, "--out", help="把每题结果（含回答原文与引用）写入 JSON 文件"),
):
    """运行测试问题集：输出前 k 命中率、MRR，以及引用有效率。"""
    from lkos.evaluation.evaluate import evaluate, load_questions

    try:
        a = open_app()
        qs = load_questions(questions)
        with console.status("评测中…"):
            rep = evaluate(qs, a.retriever, qa=a.qa if with_answer else None, k=k or a.cfg.retrieval.top_k,
                           lang=lang, mode=lang_mode, model=model)
    except LkosError as e:
        fail(e)
    table = Table("#", "问题", "命中", "名次", "检索到的文件", *(["引用", "无效"] if with_answer else []))
    for i, r in enumerate(rep.results, start=1):
        row = [str(i), r.question[:40], "[green]✓[/]" if r.hit else "[red]✗[/]", str(r.rank or "-"),
               ", ".join(r.retrieved_files[:3]) or (r.error or "-")]
        if with_answer:
            row += [str(r.valid_citations if r.valid_citations is not None else "-"),
                    str(r.invalid_citations if r.invalid_citations is not None else "-")]
        table.add_row(*row)
    console.print(table)
    console.print(f"前 {rep.k} 命中率：[bold]{rep.hit_rate:.0%}[/]（{sum(r.hit for r in rep.results)}/{rep.total}）"
                  f"　MRR：{rep.mrr:.3f}　目标 ≥ 80%")
    if with_answer:
        rate = rep.citation_rate
        console.print(f"含有效引用的回答：[bold]{(rate or 0):.0%}[/]　目标 ≥ 90%　已移除无效引用：{rep.invalid_total}")
        if rep.ttft_median_ms is not None:
            console.print(f"首 token 中位数：{rep.ttft_median_ms / 1000:.2f}s　目标 ≤ 5s")
    if out:
        import json

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"每题结果已写入：{out}")
    a.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
