"""纯文本（.txt）：自动识别编码（UTF-8 / UTF-16 / GBK / cp1252），按空行分段，引用定位到行号。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from lkos.parsers.base import decode_text
from lkos.types import Block, Location

MAX_PARA_LINES = 40       # 没有空行的长文本（如日志、聊天记录导出）每 40 行一段，避免一段过长


def text_blocks(text: str) -> Iterator[Block]:
    buf: list[str] = []
    start = 0
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines, 1):
        if line.strip():
            if not buf:
                start = i
            buf.append(line.rstrip())
            if len(buf) < MAX_PARA_LINES:
                continue
        if buf:
            yield Block(text="\n".join(buf).strip(), location=Location(line_start=start, line_end=start + len(buf) - 1))
            buf = []
    if buf:
        yield Block(text="\n".join(buf).strip(), location=Location(line_start=start, line_end=start + len(buf) - 1))


class TextParser:
    extensions = (".txt", ".text")
    file_type = "txt"
    label = "文本"

    def parse(self, path: Path) -> Iterator[Block]:
        yield from text_blocks(decode_text(Path(path).read_bytes()))
