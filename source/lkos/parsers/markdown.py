"""Markdown 解析：逐行处理，按空行分段，保留原文件行号与标题路径。

同时处理 Obsidian（以及 Logseq、Foam 等）常见写法，让检索到的是文字本身而不是标记：
- 双链 [[笔记]] / [[笔记|别名]] / [[笔记#标题]] → 显示的文字；嵌入 ![[笔记]] → “（嵌入：笔记）”，嵌入图片等附件去掉；
- 普通链接 [文字](地址) → 文字，图片 ![说明](地址) → 说明；==高亮== → 高亮的文字；
- 提示块 > [!note] 标题 → 标题；块引用标记 ^abc123 去掉；
- 属性（文件开头的 YAML）：title、tags、aliases 等写成一段可检索的“属性”，tags 同时成为文件标签；
- 仅在 Obsidian 仓库里：%%注释%% 去掉，dataview / query 等查询代码块去掉（是查询语句，不是内容），
  正文里的 #标签 也成为文件标签。
所有替换都不改变行数，引用里的行号仍然对应原文件。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

import yaml

from lkos.parsers.base import decode_text
from lkos.types import Block, Location

_ATX = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(```+|~~~+)\s*([\w-]*)")
QUERY_FENCES = {"dataview", "dataviewjs", "query", "tasks", "base"}
_EMBED = re.compile(r"!\[\[([^\]\n]+?)\]\]")
_WIKI = re.compile(r"\[\[([^\]\n]+?)\]\]")
_IMG = re.compile(r"!\[([^\]\n]*)\]\([^)\n]*\)")
_LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\((?:[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HIGHLIGHT = re.compile(r"==(?=\S)(.+?)(?<=\S)==")
_CALLOUT = re.compile(r"^\s*>\s*\[!([\w-]+)\][+-]?\s*(.*)$")
_QUOTE = re.compile(r"^\s*(?:>\s?)+")
_BLOCK_ID = re.compile(r"\s+\^[A-Za-z0-9-]+\s*$")
_COMMENT = re.compile(r"%%.*?%%")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_TAG = re.compile(r"(?<![\w#&/\\])#([^\s#!@$%^&*(),.?\":{}|<>\[\]'`，。；：！？、（）【】《》“”]+)")
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".heic", ".tif", ".tiff", ".mp3", ".m4a", ".wav",
             ".ogg", ".flac", ".mp4", ".mov", ".webm", ".mkv", ".excalidraw"}
CALLOUT_NAMES = {"note": "注", "info": "信息", "tip": "提示", "hint": "提示", "important": "重要", "warning": "注意",
                 "caution": "注意", "attention": "注意", "danger": "危险", "error": "错误", "bug": "问题",
                 "example": "示例", "quote": "引用", "cite": "引用", "question": "问题", "faq": "问题", "help": "问题",
                 "success": "完成", "check": "完成", "done": "完成", "failure": "失败", "fail": "失败",
                 "missing": "缺失", "abstract": "摘要", "summary": "摘要", "tldr": "摘要", "todo": "待办"}
PROPERTY_KEYS_SKIP = {"cssclass", "cssclasses", "publish", "permalink", "position", "kanban-plugin"}


def _link_text(inner: str) -> str:
    target, _, alias = inner.partition("|")
    if alias.strip():
        return alias.strip()
    page, _, sub = target.partition("#")
    page = page.strip()
    sub = sub.strip().lstrip("^")
    if sub and not re.fullmatch(r"[A-Za-z0-9-]+", sub) or (sub and not page):
        return f"{page} {sub}".strip() if page else sub
    return page


def _embed_text(m: re.Match) -> str:
    inner = m.group(1)
    target = inner.split("|", 1)[0].split("#", 1)[0].strip()
    if Path(target).suffix.lower() in MEDIA_EXT:
        return ""
    if Path(target).suffix.lower() == ".pdf":
        return f"（附件：{target}）"
    return f"（嵌入：{_link_text(inner)}）"


def clean_line(line: str) -> str:
    """去掉一行里的 Markdown / Obsidian 标记，只留文字（不处理标题符号、代码块）。"""
    if "[" in line or "!" in line:
        line = _EMBED.sub(_embed_text, line)
        line = _WIKI.sub(lambda m: _link_text(m.group(1)), line)
        line = _IMG.sub(lambda m: m.group(1), line)
        line = _LINK.sub(lambda m: m.group(1), line)
    if "==" in line:
        line = _HIGHLIGHT.sub(lambda m: m.group(1), line)
    if ">" in line:
        c = _CALLOUT.match(line)
        if c:
            title = c.group(2).strip()
            line = title or CALLOUT_NAMES.get(c.group(1).lower(), c.group(1))
        else:
            line = _QUOTE.sub("", line)
    if "^" in line:
        line = _BLOCK_ID.sub("", line)
    return line


def inline_tags(line: str) -> list[str]:
    if "#" not in line:
        return []
    line = _INLINE_CODE.sub(" ", line)
    return [t.strip("/") for t in _TAG.findall(line) if not t.isdigit() and t.strip("/")]


def _scalar(v) -> str | None:
    if v is None or isinstance(v, (dict,)):
        return None
    if isinstance(v, (list, tuple)):
        items = [str(x).strip() for x in v if x is not None and not isinstance(x, (dict, list))]
        return "、".join(_WIKI.sub(lambda m: _link_text(m.group(1)), x) for x in items if x) or None
    s = str(v).strip()
    return _WIKI.sub(lambda m: _link_text(m.group(1)), s) or None


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x for x in re.split(r"[,，\s]+", v) if x]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if x is not None and str(x).strip()]
    return [str(v)]


def front_matter(lines: list[str]) -> tuple[int, dict]:
    """返回 (正文开始的行下标, 属性)；没有属性时 (0, {})。"""
    if not lines or lines[0].strip() != "---":
        return 0, {}
    for j in range(1, len(lines)):
        if lines[j].strip() in ("---", "..."):
            try:
                data = yaml.safe_load("\n".join(lines[1:j])) or {}
            except yaml.YAMLError:
                data = {}
            return j + 1, data if isinstance(data, dict) else {}
    return 0, {}


class MarkdownParser:
    extensions = (".md", ".markdown")
    file_type = "md"
    label = "Markdown"  # 界面上的类型名（范围筛选、文件夹设置）

    def __init__(self):
        self.tags: list[str] = []      # 解析后：属性里的 tags + 仓库笔记正文里的 #标签（成为文件标签）

    def parse(self, path: Path) -> Iterator[Block]:
        from lkos.sources.obsidian import vault_of

        text = decode_text(Path(path).read_bytes())
        self.tags = []
        yield from parse_markdown_text(text, obsidian=vault_of(path) is not None, tags=self.tags)


def parse_markdown_text(text: str, obsidian: bool = False, tags: list[str] | None = None) -> Iterator[Block]:
    lines = text.splitlines()
    headings: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    in_fence: str | None = None
    skip_fence = False
    in_comment = False
    found: list[str] = []

    def flush(end_line: int) -> Iterator[Block]:
        nonlocal buf
        content = "\n".join(buf).strip()
        if content:
            path = " > ".join(h[1] for h in headings) or None
            yield Block(text=content, location=Location(heading_path=path, line_start=start, line_end=end_line))
        buf = []

    i, props = front_matter(lines)
    if props:
        found += [t.lstrip("#") for t in _as_list(props.get("tags") or props.get("tag"))]
        parts = []
        heads = {m.group(2).strip().lower() for m in (_ATX.match(x) for x in lines[i:]) if m}
        for k, v in props.items():
            if str(k).lower() in PROPERTY_KEYS_SKIP:
                continue
            s = _scalar(v)
            if s and s.lower() in heads:          # 和正文标题重复的 title
                continue
            if s and len(s) <= 500:
                parts.append(f"{k}：{s}")
        if parts:
            yield Block(text="属性　" + "；".join(parts), location=Location(line_start=1, line_end=i))

    while i < len(lines):
        line = lines[i]
        lineno = i + 1
        i += 1
        if obsidian and not in_fence:
            if in_comment:
                if "%%" not in line:
                    continue
                line = line.split("%%", 1)[1]
                in_comment = False
            line = _COMMENT.sub("", line)
            if "%%" in line:                     # 多行注释开始
                line, in_comment = line.split("%%", 1)[0], True
                if not line.strip():
                    continue
        fence = _FENCE.match(line)
        if in_fence:
            if fence and fence.group(1)[0] == in_fence[0] and len(fence.group(1)) >= len(in_fence) and not fence.group(2):
                in_fence = None
                if skip_fence:
                    skip_fence = False
                    continue
            if not skip_fence:
                buf.append(line)
            continue
        if fence:
            in_fence = fence.group(1)
            skip_fence = obsidian and fence.group(2).lower() in QUERY_FENCES
            if not skip_fence:
                if not buf:
                    start = lineno
                buf.append(line)
            continue
        m = _ATX.match(line)
        if m:
            yield from flush(lineno - 1)
            level, title = len(m.group(1)), clean_line(m.group(2).strip()).strip()
            headings = [h for h in headings if h[0] < level] + [(level, title)]
            continue
        if obsidian:
            found += inline_tags(line)
        cleaned = clean_line(line)
        if not cleaned.strip():
            if not line.strip() or not buf:
                yield from flush(lineno - 1)
            continue
        if not buf:
            start = lineno
        buf.append(cleaned)
    yield from flush(len(lines))
    if tags is not None:
        for t in found:
            if t and t not in tags and len(t) <= 40:
                tags.append(t)
