"""信息提取：按字段把资料里的信息整理成表格（完整设计文档第 8 节“信息提取”），每行带出处，可导出 CSV。

两种范围：
- 整个资料库：先检索与要求最相关的片段（比问答多取，默认 12 个），再分批提取；
- 单个文件：读取文件的全部文本块，分批提取。

字段：要求里写了“字段：A、B、C”就直接用；否则先让模型从要求里列出列名（一次很短的调用）。
每批让模型输出 JSON 数组，每个对象包含各字段和“来源”编号；解析失败时退回解析 Markdown 表格。
"""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable

from lkos.errors import Cancelled, LkosError
from lkos.qa.prompt import OUTPUT_MARGIN, estimate_tokens, format_source
from lkos.qa.service import answer_lang
from lkos.types import RetrievedChunk

TASK = "knowledge_qa"
COLUMN_TOKENS = 80
ROW_TOKENS = 900
SOURCE_KEY = "来源"
_FIELDS_RE = re.compile(r"(?:字段|列|columns?|fields?)\s*[:：]\s*(.+)$", re.I | re.S)
_SPLIT_RE = re.compile(r"\s*[、,，;；/|]\s*")


@dataclass
class ExtractResult:
    columns: list[str]
    rows: list[dict] = field(default_factory=list)   # {"values": {列: 值}, "refs": [片段编号]}
    context: list[RetrievedChunk] = field(default_factory=list)
    batches: int = 0
    latency_ms: int = 0


def parse_columns(request: str) -> list[str]:
    """从“……字段：客户、金额、日期”里取出列名；没写时返回空列表。"""
    m = _FIELDS_RE.search(request)
    if not m:
        return []
    cols = [c.strip(" 。.\n") for c in _SPLIT_RE.split(m.group(1)) if c.strip(" 。.\n")]
    return list(dict.fromkeys(cols))[:8]


def _json_array(text: str):
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _markdown_rows(text: str, columns: list[str]) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if line.count("|") < 2 or re.match(r"^\s*\|?\s*:?-{2,}", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[: len(columns)] == columns:
            continue
        rows.append(dict(zip(columns + [SOURCE_KEY], cells)))
    return rows


def parse_rows(text: str, columns: list[str], n_sources: int) -> list[dict]:
    """模型输出 → [{"values": {...}, "refs": [...]}]，丢掉全空的行和超出范围的来源编号。"""
    data = _json_array(text)
    raw = [d for d in data if isinstance(d, dict)] if data is not None else _markdown_rows(text, columns)
    out = []
    for d in raw:
        values = {c: str(d.get(c, "") or "").strip() for c in columns}
        if not any(values.values()):
            continue
        src = d.get(SOURCE_KEY, d.get("source", d.get("ref", "")))
        refs = sorted({int(x) for x in re.findall(r"\d+", str(src)) if 1 <= int(x) <= n_sources})
        out.append({"values": values, "refs": refs})
    return out


PROMPTS = {
    "zh": {
        "columns": "用户想从资料里整理一张表格，要求是：{request}\n请只输出这张表格的列名，用顿号“、”分隔，不超过 6 列，不要解释。",
        "system": "你是信息提取助手。只从下面编号的资料里提取信息，不要编造；资料里没有的字段留空字符串。",
        "user": "资料：\n{sources}\n\n要求：{request}\n请把符合要求的每一条信息整理成一行，输出 JSON 数组，每个元素是一个对象，"
                "包含这些键：{keys}。其中“来源”填资料编号（数字）。只输出 JSON，不要其他文字；没有符合的信息就输出 []。",
    },
    "en": {
        "columns": "The user wants a table from their documents. Request: {request}\nOutput only the column names, "
                   "separated by commas, at most 6, no explanation.",
        "system": "You extract information. Use only the numbered sources below; never invent values. "
                  "Leave a field as an empty string when the sources do not give it.",
        "user": "Sources:\n{sources}\n\nRequest: {request}\nPut every matching item on its own row and output a JSON array "
                "of objects with these keys: {keys}. Put the source number in \"来源\". Output only JSON; "
                "output [] if nothing matches.",
    },
}


class Extractor:
    def __init__(self, cfg, repo, retriever, gateway, marker=None):
        self.cfg = cfg
        self.repo = repo
        self.retriever = retriever
        self.gateway = gateway
        self.marker = marker

    def _chat(self, system: str | None, user: str, max_tokens: int, model_override=None) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
        result, _ = self.gateway.chat(TASK, messages, model_override=model_override, max_tokens=max_tokens,
                                      log_as="extract")
        return result.text

    def extract(self, request: str, path: str | None = None, k: int = 12,
                on_progress: Callable[[int, int], None] | None = None, on_context=None,
                cancelled: Callable[[], bool] | None = None, model_override: str | None = None,
                scope=None) -> ExtractResult:
        with (self.marker.active() if self.marker else nullcontext()):
            return self._run(request.strip(), path, k, on_progress or (lambda *a: None), on_context,
                             cancelled or (lambda: False), model_override, scope)

    def _run(self, request, path, k, progress, on_context, cancelled, model_override, scope=None) -> ExtractResult:
        if not request:
            raise LkosError("请写清楚要提取什么，例如“列出各项目的名称、预算和负责人”")
        P = PROMPTS["zh" if answer_lang(request) == "zh" else "en"]
        columns = parse_columns(request)
        if not columns:
            text = self._chat(None, P["columns"].format(request=request), COLUMN_TOKENS, model_override)
            columns = [c.strip(" 。.\"'") for c in _SPLIT_RE.split(text.strip().splitlines()[0] if text.strip() else "")
                       if c.strip(" 。.\"'")][:6] or ["内容"]
        if path:
            chunks = self.repo.chunks_of_file(path)
            if not chunks:
                raise LkosError("这个文件还没有索引，或者没有可读的文字")
        else:
            extra = {"scope": scope} if scope is not None and not scope.empty else {}
            chunks = self.retriever.search(request, k=k, **extra)
            if not chunks:
                return ExtractResult(columns=columns)
        for i, c in enumerate(chunks, start=1):
            c.score = c.score or 0.0
        if on_context:
            on_context(chunks)

        num_ctx = self.cfg.chat_model(self.gateway.resolve(TASK, model_override)).context_window
        keys = "、".join(f"“{c}”" for c in columns + [SOURCE_KEY])
        budget = (num_ctx - ROW_TOKENS - OUTPUT_MARGIN - estimate_tokens(P["system"] + P["user"] + request + keys) - 40)
        batches: list[list[tuple[int, RetrievedChunk]]] = []
        cur, used = [], 0
        for n, c in enumerate(chunks, start=1):
            cost = estimate_tokens(format_source(n, c)) + 2
            if cur and used + cost > budget:
                batches.append(cur)
                cur, used = [], 0
            cur.append((n, c))
            used += cost
        if cur:
            batches.append(cur)

        result = ExtractResult(columns=columns, context=chunks, batches=len(batches))
        seen = set()
        for i, batch in enumerate(batches, start=1):
            if cancelled():
                raise Cancelled("已停止提取")
            progress(i - 1, len(batches))
            sources = "\n\n".join(format_source(n, c) for n, c in batch)
            text = self._chat(P["system"], P["user"].format(sources=sources, request=request, keys=keys),
                              ROW_TOKENS, model_override)
            for row in parse_rows(text, columns, len(chunks)):
                sig = tuple(row["values"][c] for c in columns)
                if sig in seen:
                    continue
                seen.add(sig)
                result.rows.append(row)
        progress(len(batches), len(batches))
        return result


def to_markdown(columns: list[str], rows: list[dict]) -> str:
    """表格的文字版（存入问答记录、复制用）：出处写成 [编号]。"""
    def cell(v: str) -> str:
        return v.replace("|", "/").replace("\n", " ")

    lines = ["| " + " | ".join(columns + ["出处"]) + " |", "|" + " --- |" * (len(columns) + 1)]
    for r in rows:
        refs = "".join(f"[{n}]" for n in r["refs"])
        lines.append("| " + " | ".join(cell(r["values"][c]) for c in columns) + f" | {refs} |")
    return "\n".join(lines)


def to_csv(columns: list[str], rows: list[dict], context: list[dict]) -> str:
    """CSV（带 BOM，Excel 直接打开不乱码）：最后两列是出处文件与位置。"""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns + ["出处文件", "出处位置"])
    by_n = {c["n"]: c for c in context}
    for r in rows:
        files = "；".join(dict.fromkeys(by_n[n]["file"] for n in r["refs"] if n in by_n))
        locs = "；".join(by_n[n]["loc"] for n in r["refs"] if n in by_n and by_n[n]["loc"])
        w.writerow([r["values"][c] for c in columns] + [files, locs])
    return "﻿" + buf.getvalue()
