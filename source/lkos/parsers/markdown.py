"""Markdown 解析：逐行处理，按空行分段，保留原文件行号与标题路径。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from lkos.errors import ParseError
from lkos.types import Block, Location

_ATX = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


class MarkdownParser:
    extensions = (".md", ".markdown")
    file_type = "md"
    label = "Markdown"  # 界面上的类型名（范围筛选、文件夹设置）

    def parse(self, path: Path) -> Iterator[Block]:
        raw = Path(path).read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gb18030")  # 部分中文 Markdown 保存为 GBK
            except UnicodeDecodeError as e:
                raise ParseError(f"无法识别文件编码：{e}") from e
        yield from parse_markdown_text(text)


def parse_markdown_text(text: str) -> Iterator[Block]:
    lines = text.splitlines()
    headings: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    in_fence: str | None = None

    def flush(end_line: int) -> Iterator[Block]:
        nonlocal buf
        content = "\n".join(buf).strip()
        if content:
            path = " > ".join(h[1] for h in headings) or None
            yield Block(text=content, location=Location(heading_path=path, line_start=start, line_end=end_line))
        buf = []

    i = 0
    # YAML front matter
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                i = j + 1
                break

    while i < len(lines):
        line = lines[i]
        lineno = i + 1
        fence = _FENCE.match(line)
        if in_fence:
            buf.append(line)
            if fence and fence.group(1) == in_fence:
                in_fence = None
            i += 1
            continue
        if fence:
            if not buf:
                start = lineno
            in_fence = fence.group(1)
            buf.append(line)
            i += 1
            continue
        m = _ATX.match(line)
        if m:
            yield from flush(lineno - 1)
            level, title = len(m.group(1)), m.group(2).strip()
            headings = [h for h in headings if h[0] < level] + [(level, title)]
        elif not line.strip():
            yield from flush(lineno - 1)
        else:
            if not buf:
                start = lineno
            buf.append(line)
        i += 1
    yield from flush(len(lines))
