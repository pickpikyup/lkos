"""命令行入口：只负责解析参数和显示结果，业务逻辑都在服务层。"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from lkos.app import CONFIG_NAME, App
from lkos.config import default_home
from lkos.errors import LkosError
from lkos.types import Answer, ProgressEvent

app = typer.Typer(add_completion=False, no_args_is_help=False,
                  help="Local Knowledge OS：本地知识库索引、检索与问答。不带参数运行进入交互式问答。")
source_app = typer.Typer(add_completion=False, no_args_is_help=True, help="管理要检索的文件夹。")
app.add_typer(source_app, name="source")
daemon_app = typer.Typer(add_completion=False, no_args_is_help=True,
                         help="后台服务：关掉窗口后继续按周期更新索引（macOS，登录后自动运行）。")
app.add_typer(daemon_app, name="daemon")
console = Console()
err = Console(stderr=True)


class State:
    home: Optional[Path] = None
    config: Optional[Path] = None
    app: Optional[App] = None


state = State()


@app.callback(invoke_without_command=True)
def _global(
    ctx: typer.Context,
    home: Optional[Path] = typer.Option(None, "--home", envvar="LKOS_HOME",
                                        help="数据目录（数据库、配置、缓存）。默认为项目目录下的 data/。"),
    config: Optional[Path] = typer.Option(None, "--config", help="配置文件路径，默认 <home>/config.yaml。"),
):
    global console, err
    console, err = Console(), Console(stderr=True)  # 每次调用重新读取终端宽度等环境
    state.home = home
    state.config = config
    if ctx.invoked_subcommand is None:
        shell()


def shell() -> None:
    """交互式问答（不带参数运行 lkos 时进入）。"""
    from lkos.shell import Shell

    try:
        a = open_app()
    except LkosError as e:
        fail(e)
    try:
        Shell(a, console, err).run()
    finally:
        a.close()
    raise typer.Exit()


@app.command("app")
def app_cmd(debug: bool = typer.Option(False, "--debug", help="打开网页检查器（排查界面问题）")):
    """打开独立窗口应用（问答、搜索、资料库）。"""
    from lkos.ui.window import run

    try:
        run(home=state.home, config_path=state.config, debug=debug)
    except LkosError as e:
        fail(e)


# ---------------- source / update ----------------
@source_app.command("add")
def source_add(folder: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True,
                                             help="要检索的文件夹")):
    """添加文件夹并立即建索引（Ctrl-C 可停下，之后 lkos update 继续）。"""
    index(folder=folder, limit=None, when_idle=False)


@source_app.command("list")
def source_list():
    """列出已添加的文件夹。"""
    try:
        a = open_app()
    except LkosError as e:
        fail(e)
    rows = a.repo.list_sources()
    a.close()
    if not rows:
        console.print("还没有添加文件夹：lkos source add <文件夹>")
        return
    table = Table("编号", "文件夹", "文件", "文本块", "失败", "最近更新（UTC）")
    for i, r in enumerate(rows, start=1):
        table.add_row(str(i), r["path"], str(r["files"]), str(r["chunks"]), str(r["failed"]),
                      (r["last_indexed"] or "从未完成")[:16].replace("T", " "))
    console.print(table)


@source_app.command("remove")
def source_remove(number: int = typer.Argument(..., min=1, help="编号（见 lkos source list）"),
                  yes: bool = typer.Option(False, "--yes", "-y", help="不再确认")):
    """移除文件夹及其索引（不会删除原文件）。"""
    try:
        a = open_app()
    except LkosError as e:
        fail(e)
    rows = a.repo.list_sources()
    if number > len(rows):
        a.close()
        err.print(f"没有编号 {number}，共 {len(rows)} 个文件夹。")
        raise typer.Exit(code=1)
    r = rows[number - 1]
    if not yes and not typer.confirm(f"移除 {r['path']} 的索引？原文件不受影响"):
        a.close()
        raise typer.Exit()
    n = a.repo.remove_source(r["id"])
    a.close()
    console.print(f"已移除 {r['path']}，删除了 {n} 个文件的索引。")


@source_app.command("set")
def source_set(number: int = typer.Argument(..., min=1, help="编号（见 lkos source list）"),
               refresh: Optional[str] = typer.Option(None, "--refresh", help="自动更新周期：daily / weekly / monthly / manual"),
               exclude: Optional[list[str]] = typer.Option(None, "--exclude", "-x",
                                                           help="排除规则，可写多次，如 -x 归档 -x '2019/*'；写 -x '' 清空"),
               skip_type: Optional[list[str]] = typer.Option(None, "--skip-type", help="不索引的扩展名，如 --skip-type ppt"),
               ocr: Optional[bool] = typer.Option(None, "--ocr/--no-ocr", help="扫描版 PDF 文字识别（macOS）")):
    """查看或修改文件夹设置（不带选项时只显示当前设置）。改了排除规则后运行 lkos update 生效。"""
    try:
        a = open_app()
    except LkosError as e:
        fail(e)
    try:
        rows = a.repo.list_sources()
        if number > len(rows):
            err.print(f"没有编号 {number}，共 {len(rows)} 个文件夹。")
            raise typer.Exit(code=1)
        r = rows[number - 1]
        ex = None
        if exclude is not None or skip_type is not None:
            ex = {"patterns": [x for x in (exclude if exclude is not None else r["exclude"]["patterns"]) if x],
                  "types": [x for x in (skip_type if skip_type is not None else r["exclude"]["types"]) if x]}
        try:
            a.repo.update_source_settings(r["id"], refresh=refresh, exclude=ex, ocr=ocr)
        except ValueError as e:
            err.print(str(e))
            raise typer.Exit(code=1)
        s = a.repo.source_settings(r["path"])
    finally:
        a.close()
    names = {"daily": "每天", "weekly": "每周", "monthly": "每月", "manual": "只手动"}
    console.print(f"[bold]{s['path']}[/bold]")
    console.print(f"  自动更新：{names.get(s['refresh'], s['refresh'])}")
    console.print(f"  排除规则：{'、'.join(s['exclude']['patterns']) or '无'}")
    console.print(f"  不索引类型：{'、'.join(s['exclude']['types']) or '无'}")
    console.print(f"  扫描件文字识别：{'开' if s['ocr'] else '关'}")


@daemon_app.command("run")
def daemon_run(once: bool = typer.Option(False, "--once", help="只检查并更新一轮就退出")):
    """在前台运行后台服务（launchd 调用的就是它）。"""
    from lkos.daemon import run_daemon

    try:
        n = run_daemon(home=state.home, config_path=state.config, once=once)
    except LkosError as e:
        fail(e)
    if once:
        console.print(f"本轮更新了 {n} 个文件夹。")


@daemon_app.command("install")
def daemon_install():
    """安装并启动后台服务（写入 ~/Library/LaunchAgents/com.lkos.indexer.plist）。"""
    from lkos import daemon

    try:
        a = open_app()
        home = a.home
        a.close()
        path = daemon.install(home)
    except LkosError as e:
        fail(e)
    console.print(f"已安装后台服务：{path}\n关掉窗口后会在接通电源、电脑空闲时按各文件夹的周期更新索引。")


@daemon_app.command("uninstall")
def daemon_uninstall():
    """停止并卸载后台服务。"""
    from lkos import daemon

    console.print("已卸载后台服务。" if daemon.uninstall() else "后台服务没有安装。")


@daemon_app.command("status")
def daemon_status():
    """查看后台服务是否安装、是否在运行、最近一次更新。"""
    from lkos import daemon

    try:
        a = open_app()
        home = a.home
        a.close()
    except LkosError as e:
        fail(e)
    st = daemon.status(home)
    states = {"idle": "等待中", "running": "正在更新", "app-open": "应用已打开，由应用负责索引", "stopped": "没有运行"}
    state_text = states.get(st["state"], st["state"] or "")
    if (st["state"] or "").startswith("paused:"):
        state_text = "已暂停（" + {"qa": "正在问答", "battery": "使用电池", "busy": "你在使用电脑"}.get(st["state"][7:], st["state"][7:]) + "）"
    console.print(f"已安装：{'是' if st['installed'] else '否'}" + ("" if st["supported"] else "（只支持 macOS）"))
    console.print(f"状态：{state_text}" + (f"：{st['folder']}" if st.get("folder") else ""))
    last = st.get("last") or {}
    if last:
        console.print("最近一次：" + (f"{last.get('folder')} 出错：{last['error']}" if last.get("error") else
                                   f"{last.get('folder')}：新索引 {last.get('indexed', 0)}、删除 {last.get('removed', 0)}"
                                   f"（{(last.get('at') or '')[:16].replace('T', ' ')} UTC）"))


@app.command()
def update(when_idle: bool = typer.Option(False, "--when-idle", help="闲时索引：问答、用电池、使用电脑时暂停")):
    """更新所有已添加文件夹的索引（只处理新增、修改、删除的文件）。"""
    try:
        a = open_app()
    except LkosError as e:
        fail(e)
    rows = a.repo.list_sources()
    if not rows:
        a.close()
        console.print("还没有添加文件夹：lkos source add <文件夹>")
        return
    try:
        for r in rows:
            folder = Path(r["path"])
            if not folder.is_dir():
                console.print(f"[yellow]跳过（找不到文件夹）：{folder}[/]")
                continue
            console.print(f"[bold]{folder}[/]")
            rep, gate = run_index(a, folder, when_idle=when_idle)
            print_index_report(rep, gate, brief=True)
    except KeyboardInterrupt:
        err.print("已中断。已完成的文件已保存，重新运行即可续传。")
        raise typer.Exit(code=130)
    except LkosError as e:
        fail(e)
    finally:
        a.close()


def open_app() -> App:
    from lkos.errors import SchemaVersionError

    try:
        state.app = App.open(home=state.home, config_path=state.config)
    except SchemaVersionError as e:
        # 旧版本建的索引：自动改名备份并换成新格式，保留原来添加的文件夹，之后 update 重建
        backup, sources = App.retire_old_index(state.home)
        err.print(f"[yellow]{str(e).split('。')[0]}。旧索引已备份为 {backup.name}，已换成新格式。[/]")
        state.app = App.open(home=state.home, config_path=state.config)
        for path in sources:
            state.app.repo.upsert_source(path)
        if sources:
            err.print(f"[yellow]已保留原来的 {len(sources)} 个文件夹，运行 /update（或 lkos update）重新建索引。[/]")
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
    when_idle: bool = typer.Option(False, "--when-idle",
                                   help="闲时索引：正在问答、使用电池、或你在用电脑时自动暂停，条件满足后继续"),
):
    """索引文件夹（增量：未变化的文件跳过，改名的文件不重新计算 Embedding；最近修改的文件优先）。"""
    try:
        a = open_app()
    except LkosError as e:
        fail(e)
    try:
        rep, gate = run_index(a, folder, limit=limit, when_idle=when_idle)
    except KeyboardInterrupt:
        err.print("已中断。已完成的文件已保存，重新运行即可续传。")
        raise typer.Exit(code=130)
    except LkosError as e:
        fail(e)
    finally:
        a.close()
    print_index_report(rep, gate)


def run_index(a: App, folder: Path, limit: Optional[int] = None, when_idle: bool = False):
    """带进度条地索引一个文件夹，返回 (IndexReport, 闲时闸门或 None)。Ctrl-C 与错误交给调用方处理。"""
    from lkos.daemon import index_lock, wait_for_index_lock

    folder = Path(folder).expanduser().resolve()
    lock = index_lock(a.home, "cli")
    wait_for_index_lock(lock, on_wait=lambda: err.print("应用或后台服务正在索引，等它完成当前文件后继续…"))
    try:
        return _run_index_locked(a, folder, limit, when_idle)
    finally:
        lock.release()


def _run_index_locked(a: App, folder: Path, limit: Optional[int], when_idle: bool):
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

    total = sum(1 for _ in a.indexer.walk(folder))
    if limit:
        total = min(total, limit)
    labels = {"indexed": "已索引", "unchanged": "未变化", "moved": "已移动", "skipped": "已跳过", "failed": "失败"}
    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(),
                  console=console, transient=True) as progress:
        task = progress.add_task("索引中", total=total)

        def on_progress(ev: ProgressEvent) -> None:
            progress.update(task, completed=ev.files_seen,
                            description=f"{labels.get(ev.action, ev.action)} {Path(ev.path).name[:40]}")

        gate = None
        if when_idle:
            from lkos.indexing.scheduler import REASONS

            def on_pause(reason: str) -> None:
                progress.console.print(f"[dim]{time.strftime('%H:%M:%S')}[/] {REASONS[reason]}")

            gate = a.index_gate(on_pause=on_pause)
        rep = a.indexer.index_folder(folder, limit=limit, on_progress=on_progress, gate=gate)
    return rep, gate


def print_index_report(rep, gate=None, brief: bool = False) -> None:
    if brief:
        console.print(f"扫描 {rep.files_seen} 个文件：新索引 {rep.files_indexed} · 未变化/跳过 {rep.files_skipped} · "
                      f"改名 {rep.files_moved} · 失败 {rep.files_failed} · 已删除 {rep.files_removed} · "
                      f"新增文本块 {rep.chunks_added} · 用时 {rep.seconds:.0f} 秒")
    else:
        table = Table("项目", "数量", show_header=False)
        for label, value in [("扫描文件", rep.files_seen), ("新索引", rep.files_indexed),
                             ("未变化/跳过", rep.files_skipped),
                             ("改名/移动", rep.files_moved), ("失败", rep.files_failed), ("已删除", rep.files_removed),
                             ("新增文本块", rep.chunks_added), ("其中复用缓存向量", rep.chunks_reused),
                             ("清理无用向量", rep.vectors_pruned), ("耗时（秒）", rep.seconds),
                             ("　解析（秒）", rep.parse_seconds), ("　计算向量（秒）", rep.embed_seconds)]:
            table.add_row(label, str(value))
        console.print(table)
        if gate is not None:
            p = gate.pauses
            console.print(f"闲时索引：暂停共 {gate.paused_seconds:.0f} 秒（问答 {p['qa']} 次 · 用户使用电脑 {p['busy']} 次 · "
                          f"使用电池 {p['battery']} 次）")
        if rep.files_indexed and rep.seconds:
            console.print(f"吞吐：{rep.files_indexed / rep.seconds:.2f} 文件/秒，"
                          f"{rep.chunks_added / rep.seconds:.1f} 文本块/秒")
    if rep.rechunked:
        console.print("[yellow]切块参数已变化，本次重新切块了这个文件夹的所有文件（未改动文本的向量已复用）。[/]")
    if not rep.cleanup_done:
        console.print("[yellow]使用了 --limit，本次未清理已删除的文件。[/]")
    for path, reason in rep.failures[:10]:
        err.print(f"[red]失败[/] {path}：{reason}", highlight=False)


# ---------------- search ----------------
@app.command()
def search(
    query: str = typer.Argument(..., help="检索内容"),
    k: Optional[int] = typer.Option(None, "--k", min=1, max=50, help="返回片段数，默认取配置 retrieval.top_k"),
    lang: str = typer.Option("auto", "--lang", help="auto | zh | en | fr | all"),
    lang_mode: Optional[str] = typer.Option(None, "--lang-mode", help="soft（默认，同语言加权）| strict（只检索同语言）| all"),
    strategy: Optional[str] = typer.Option(None, "--strategy", help="doc_first（默认，先选文档再选段落）| flat（v0.2 做法）"),
):
    """只检索不生成：显示前 k 个片段、分数和位置。"""
    try:
        a = open_app()
        hits = a.retriever.search(query, k=k, lang=lang, mode=lang_mode, strategy=strategy)
        dbg = a.retriever.last_debug
    except LkosError as e:
        fail(e)
    console.print(f"问题语言：{dbg.lang}　模式：{dbg.mode}　过滤：{dbg.lang_filter or '不过滤'}　"
                  f"策略：{dbg.strategy}　关键词：{dbg.fts_query or '（无）'}")
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
              stream: bool, mode: Optional[str] = None, strategy: Optional[str] = None) -> None:
    on_token = (lambda t: (sys.stdout.write(t), sys.stdout.flush())) if stream else None
    ans = a.qa.ask(question, k=k, model_override=model, lang=lang, on_token=on_token, mode=mode, strategy=strategy)
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
    strategy: Optional[str] = typer.Option(None, "--strategy", help="doc_first（默认，先选文档再选段落）| flat（v0.2 做法）"),
):
    """检索 + 生成：输出带引用的回答。"""
    try:
        a = open_app()
        _ask_once(a, question, k, model, lang, show_context, stream, lang_mode, strategy)
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
    strategy: Optional[str] = typer.Option(None, "--strategy", help="doc_first（默认，先选文档再选段落）| flat（v0.2 做法）"),
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
            _ask_once(a, q, k, model, lang, show_context, stream, lang_mode, strategy)
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
    strategy: Optional[str] = typer.Option(None, "--strategy", help="doc_first（默认，先选文档再选段落）| flat（v0.2 做法）"),
):
    """运行测试问题集：文件级命中率、MRR、块级命中率；加 --with-answer 时还有答案正确率、引用率、首 token。"""
    from lkos.evaluation.evaluate import evaluate, load_questions

    try:
        a = open_app()
        qs = load_questions(questions)
        with console.status("评测中…"):
            rep = evaluate(qs, a.retriever, qa=a.qa if with_answer else None, k=k or a.cfg.retrieval.top_k,
                           lang=lang, mode=lang_mode, model=model, strategy=strategy)
    except LkosError as e:
        fail(e)
    def mark(v):
        return "-" if v is None else ("[green]✓[/]" if v else "[red]✗[/]")

    table = Table("#", "问题", "文件", "名次", "段落", "检索到的文件", *(["答对", "引用", "无效"] if with_answer else []))
    for i, r in enumerate(rep.results, start=1):
        row = [str(i), r.question[:40], mark(r.hit), str(r.rank or "-"), mark(r.chunk_hit),
               ", ".join(r.retrieved_files[:3]) or (r.error or "-")]
        if with_answer:
            row += [mark(r.answer_correct) if not r.not_found or r.answer_correct else "没找到",
                    str(r.valid_citations if r.valid_citations is not None else "-"),
                    str(r.invalid_citations if r.invalid_citations is not None else "-")]
        table.add_row(*row)
    console.print(table)
    strategy_used = strategy or a.cfg.retrieval.strategy
    console.print(f"策略：{strategy_used}　前 {rep.k} 文件级命中率：[bold]{rep.hit_rate:.0%}[/]"
                  f"（{sum(r.hit for r in rep.results)}/{rep.total}）　MRR：{rep.mrr:.3f}")
    if rep.chunk_hit_rate is not None:
        n = len(rep.with_location)
        console.print(f"块级命中率：[bold]{rep.chunk_hit_rate:.0%}[/]（{sum(r.chunk_hit for r in rep.with_location)}/{n}，"
                      f"只统计写了位置的题）　目标 ≥ 80%")
    if with_answer:
        rate = rep.citation_rate
        if rep.answer_accuracy is not None:
            console.print(f"答案正确率：[bold]{rep.answer_accuracy:.0%}[/]（只统计写了 expected_answer 的题）")
        console.print(f"含有效引用的回答：[bold]{(rate or 0):.0%}[/]　目标 ≥ 90%　已移除无效引用：{rep.invalid_total}")
        console.print(f"文档对但答“没有找到”：{rep.doc_ok_not_found} 题　无出处回答：{rep.unsupported} 题")
        if rep.ttft_median_ms is not None:
            by_lang = "　".join(f"{k} {v / 1000:.2f}s" for k, v in rep.ttft_median_by_lang.items())
            console.print(f"首 token 中位数：{rep.ttft_median_ms / 1000:.2f}s（{by_lang}）　目标：各语言 ≤ 5s")
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
