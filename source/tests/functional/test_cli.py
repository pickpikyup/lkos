"""功能测试：以真实子进程运行 `python -m lkos`，按 POC 验收标准逐项验证端到端行为。

模型使用 FakeProvider（通过配置文件 type: fake 指定），不依赖 Ollama。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.conftest import fake_overrides
from tests.samples import make_docx

ROOT = Path(__file__).resolve().parents[2]


class Cli:
    """默认在进程内用 CliRunner 调用（快，且计入覆盖率）；subprocess=True 时启动真实子进程。"""

    def __init__(self, home: Path, tmp: Path, subprocess: bool = False):
        self.home = home
        self.subprocess = subprocess
        self.env = {
            **os.environ,
            "LKOS_HOME": str(home),
            "COLUMNS": "220",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(tmp),
            "NO_COLOR": "1",
        }

    def __call__(self, *args: str, input: str | None = None, expect: int = 0) -> str:
        if not self.subprocess:
            from typer.testing import CliRunner

            from lkos.cli import app

            env = {k: v for k, v in self.env.items() if k in ("LKOS_HOME", "COLUMNS", "NO_COLOR")}
            res = CliRunner().invoke(app, list(args), input=input, env=env, catch_exceptions=False)
            out = res.output
            assert res.exit_code in _codes(expect), f"exit={res.exit_code}\n{out}"
            return out
        proc = subprocess.run([sys.executable, "-m", "lkos", *args], cwd=ROOT, env=self.env, input=input,
                              capture_output=True, text=True, timeout=180)
        out = proc.stdout + proc.stderr
        assert proc.returncode in _codes(expect), f"exit={proc.returncode}\n{out}"
        return out


def _codes(expect) -> tuple:
    return tuple(expect) if isinstance(expect, (tuple, list)) else (expect,)


def write_config(home: Path, **extra) -> None:
    home.mkdir(parents=True, exist_ok=True)
    cfg = fake_overrides(**extra)
    cfg["providers"]["local"] = {"type": "ollama", "base_url": "http://127.0.0.1:9"}  # 保证连不上
    (home / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def cli(tmp_path):
    home = tmp_path / "home"
    write_config(home)
    return Cli(home, tmp_path)


def test_init_reports_models_ready(cli):
    out = cli("init")
    assert "test/fake-embed" in out and "就绪" in out
    assert (cli.home / "lkos.db").exists()


def test_init_with_default_config_reports_ollama_down(tmp_path):
    home = tmp_path / "fresh"
    home.mkdir()
    (home / "config.yaml").write_text("providers:\n  local:\n    base_url: http://127.0.0.1:9\n", encoding="utf-8")
    out = Cli(home, tmp_path)("init", expect=2)
    assert "local/qwen3:4b-instruct" in out and "local/bge-m3" in out and "无法连接 Ollama" in out


def test_init_writes_default_config_when_missing(tmp_path):
    home = tmp_path / "brand-new"
    # 默认配置指向本机 Ollama：本机有 Ollama 和模型时退出码为 0，没有时为 2，两种都正常
    out = Cli(home, tmp_path)("init", expect=(0, 2))
    assert "已写入默认配置" in out
    assert "qwen3:4b-instruct" in (home / "config.yaml").read_text("utf-8")


def test_full_flow_index_search_ask_status(cli, corpus):
    out = cli("index", str(corpus))
    assert "新索引" in out and "| 4" not in out  # 表格无边框字符依赖
    assert "吞吐" in out

    out = cli("search", "项目二期的预算上限是多少？")
    assert "问题语言：zh" in out and "模式：soft" in out and "过滤：不过滤" in out
    out = cli("search", "项目二期的预算上限是多少？", "--lang-mode", "strict")
    assert "模式：strict" in out and "过滤：zh" in out
    assert "项目计划书.docx" in out or "评审会议纪要.pdf" in out

    out = cli("ask", "项目二期的预算上限是多少？", "--show-context")
    assert "根据资料" in out and "引用" in out and "[1]" in out
    assert "送入模型的片段" in out and "首 token" in out and "可用内存" in out

    out = cli("ask", "What is the retention policy for logs?", "--no-stream", "--strategy", "flat")
    assert "security-policy.md" in out and "Security Policy > Log retention" in out
    # doc_first：最佳片段补入了相邻块，引用位置变成合并后的标题与行号范围
    out = cli("ask", "What is the retention policy for logs?", "--no-stream", "--strategy", "doc_first")
    assert "security-policy.md · Security Policy · 行 " in out

    out = cli("status")
    assert "'indexed': 4" in out and "'zh'" in out and "'fr'" in out and "远端调用" in out


def test_reindex_skips_unchanged(cli, corpus):
    cli("index", str(corpus))
    out = cli("index", str(corpus))
    assert _row(out, "新索引") == "0" and _row(out, "未变化/跳过") == "4"


def test_incremental_add_modify_delete_rename(cli, corpus):
    cli("index", str(corpus))
    (corpus / "security-policy.md").write_text("# Policy\n\nLogs are kept for thirty days.\n", encoding="utf-8")
    make_docx(corpus / "新增通知.docx", [("h1", "通知"), ("p", "公司年会定于十二月在杭州举办。")])
    (corpus / "fr" / "guide-accueil.md").unlink()
    (corpus / "评审会议纪要.pdf").rename(corpus / "评审会议纪要-终版.pdf")
    out = cli("index", str(corpus))
    assert _row(out, "新索引") == "2"       # 修改 1 + 新增 1
    assert _row(out, "改名/移动") == "1"
    assert _row(out, "已删除") == "1"
    assert _row(out, "未变化/跳过") == "1"   # 项目计划书.docx
    out = cli("search", "年会在哪里举办")
    assert "新增通知.docx" in out
    out = cli("search", "评审结论")
    assert "评审会议纪要-终版.pdf" in out


def test_index_limit(cli, corpus):
    out = cli("index", str(corpus), "--limit", "2")
    assert _row(out, "扫描文件") == "2" and "未清理已删除" in out


def test_failed_files_listed(cli, corpus):
    (corpus / "broken.docx").write_bytes(b"PK\x03\x04garbage")
    out = cli("index", str(corpus))
    assert _row(out, "失败") == "1" and "broken.docx" in out
    out = cli("status", "--failed")
    assert "failed" in out and "broken.docx" in out


def test_invalid_citations_removed(tmp_path, corpus):
    home = tmp_path / "h"
    write_config(home, providers={"test": {"type": "fake", "invalid_ref": True}})
    cli = Cli(home, tmp_path)
    cli("index", str(corpus))
    out = cli("ask", "项目二期的预算上限是多少？", "--no-stream")
    assert "已移除 1 个无效引用：[99]" in out and "[99]" not in out.split("已移除")[0]


def test_eval_command(cli, corpus, tmp_path):
    cli("index", str(corpus))
    q = tmp_path / "questions.yaml"
    q.write_text(yaml.safe_dump([
        {"question": "项目二期的预算上限是多少？", "expected_files": ["项目计划书.docx", "评审会议纪要.pdf"]},
        {"question": "What is the retention policy for logs?", "expected_files": ["security-policy.md"]},
        {"question": "Où se trouve la machine à café ?", "expected_files": ["guide-accueil.md"]},
    ], allow_unicode=True), encoding="utf-8")
    out = cli("eval", str(q))
    assert "文件级命中率：100%（3/3）" in out
    res = tmp_path / "out" / "results.json"
    out = cli("eval", str(q), "--with-answer", "--model", "test/other-chat", "--out", str(res))
    assert "含有效引用的回答：100%" in out and "首 token 中位数" in out
    import json

    data = json.loads(res.read_text("utf-8"))
    assert data["model"] == "test/other-chat" and data["citation_rate"] == 1.0
    first = data["results"][0]
    assert first["answer"].startswith("根据资料") and first["cited"][0].startswith("[1] ")
    assert data["strategy"] is None and "chunk_hit_rate" in data and "ttft_median_by_lang" in data


def test_eval_chunk_level_and_strategy(cli, corpus, tmp_path):
    cli("index", str(corpus))
    q = tmp_path / "q2.yaml"
    q.write_text(yaml.safe_dump([
        {"question": "项目二期的预算上限是多少？", "expected_files": ["项目计划书.docx", "评审会议纪要.pdf"],
         "expected_text": "二期预算上限为一百二十万元", "expected_answer": ["一百二十万"]},
        {"question": "What is the retention policy for logs?", "expected_files": ["security-policy.md"]},
    ], allow_unicode=True), encoding="utf-8")
    out = cli("eval", str(q), "--strategy", "flat")
    assert "策略：flat" in out and "块级命中率：100%（1/1" in out
    out = cli("eval", str(q), "--with-answer", "--strategy", "doc_first")
    assert "策略：doc_first" in out and "答案正确率：" in out and "文档对但答“没有找到”：0 题" in out
    assert "首 token 中位数" in out and "zh " in out


def test_ask_empty_index(cli):
    out = cli("ask", "Where is the coffee machine?")
    assert "No relevant documents" in out


def test_remote_model_blocked_offline(cli, corpus):
    cli("index", str(corpus))
    out = cli("ask", "预算上限", "--model", "hermes/some-model", expect=1)
    assert "离线模式" in out


def test_local_model_down_errors_without_fallback(tmp_path, corpus):
    home = tmp_path / "h"
    write_config(home, tasks={"knowledge_qa": {"default_model": "local/qwen3:4b-instruct"}},
                 privacy={"remote_enabled": True, "offline_mode": False})
    cli = Cli(home, tmp_path)
    cli("index", str(corpus))  # Embedding 仍由 FakeProvider 提供
    out = cli("ask", "预算上限", expect=1)
    assert "无法连接 Ollama" in out
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(home / "lkos.db")) as conn:
        rows = conn.execute("SELECT provider, error FROM call_logs WHERE task = 'knowledge_qa'").fetchall()
    assert rows and all(p == "local" and e for p, e in rows)  # 失败已记录，且没有调用远端


def test_chat_session(cli, corpus):
    cli("index", str(corpus))
    out = cli("chat", "--no-stream", input="项目二期的预算上限是多少？\n\nWhat is the retention policy for logs?\nexit\n")
    assert out.count("引用") >= 2 and "security-policy.md" in out


def test_embedding_mismatch_reported(cli, corpus):
    cli("index", str(corpus))
    cfg = yaml.safe_load((cli.home / "config.yaml").read_text("utf-8"))
    cfg["models"]["embedding"]["dim"] = 32
    (cli.home / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    out = cli("status", expect=1)
    assert "Embedding" in out and "删除" in out


def test_real_subprocess_full_flow(tmp_path, corpus):
    """以真实子进程跑一遍主流程，验证入口点、模块加载和退出码。"""
    home = tmp_path / "sub"
    write_config(home)
    cli = Cli(home, tmp_path, subprocess=True)
    cli("init")
    cli("index", str(corpus))
    out = cli("ask", "项目二期的预算上限是多少？")
    assert "引用" in out and "[1]" in out
    out = cli("ask", "预算", "--model", "hermes/x", expect=1)
    assert "离线模式" in out


def test_help_lists_all_commands(tmp_path):
    out = Cli(tmp_path / "h", tmp_path, subprocess=True)("--help")
    for cmd in ("init", "index", "search", "ask", "chat", "status", "eval"):
        assert cmd in out


def _row(out: str, label: str) -> str:
    """从无边框的统计表中取出某一行的值。"""
    for line in out.splitlines():
        cells = [c.strip() for c in line.replace("│", "|").split("|") if c.strip()]
        if not cells:
            continue
        if cells[0] == label and len(cells) > 1:
            return cells[1]
        parts = line.split()
        if parts and parts[0].strip("│| ") == label:
            return parts[-1].strip("│| ")
    raise AssertionError(f"输出中没有“{label}”一行：\n{out}")


def test_index_when_idle(cli, corpus):
    """闲时模式：在测试环境里（非 macOS，视为已接电源且空闲）不应暂停，正常完成并汇报暂停统计。"""
    out = cli("index", str(corpus), "--when-idle")
    assert _row(out, "新索引") == "4" and "闲时索引：暂停共 0 秒" in out


def test_source_commands_and_update(cli, corpus):
    out = cli("source", "list")
    assert "还没有添加文件夹" in out
    out = cli("source", "add", str(corpus))
    assert _row(out, "新索引") == "4"
    out = cli("source", "list")
    assert str(corpus) in out and "4" in out
    (corpus / "新文件.md").write_text("# 新文件\n\n年会在三楼举办。\n", encoding="utf-8")
    out = cli("update")
    assert "新索引 1" in out
    out = cli("source", "remove", "1", "--yes")
    assert "删除了 5 个文件的索引" in out
    assert "还没有添加文件夹" in cli("source", "list")
    assert "还没有添加文件夹" in cli("update")
