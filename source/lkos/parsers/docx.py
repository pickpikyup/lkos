"""DOCX 解析：按正文元素顺序（段落、表格行）抽取，定位到标题路径 + 元素序号。

docx 没有固定页码，因此不提供页码。
标题识别同时支持英文样式名（Heading 1）、中文样式名（标题 1，WPS 常见）和大纲级别。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

import docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from lkos.errors import EncryptedFileError, ParseError
from lkos.parsers.base import clean_text
from lkos.types import Block, Location

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # 加密 docx 或旧版 .doc 都是 OLE 容器
_HEADING_RE = re.compile(r"^(?:heading|标题)\s*([1-9])$", re.IGNORECASE)


def heading_level(paragraph: Paragraph) -> int | None:
    style = paragraph.style
    if style is not None and style.name:
        m = _HEADING_RE.match(style.name.strip())
        if m:
            return int(m.group(1))
        if style.name.strip().lower() in ("title", "标题"):
            return 1
    # 大纲级别：段落自身优先，其次沿样式继承链查找
    lvl = _outline_level(paragraph._p.pPr)
    s = style
    while lvl is None and s is not None:
        lvl = _outline_level(s.element.pPr)
        s = s.base_style
    if lvl is not None and 0 <= lvl <= 8:
        return lvl + 1
    return None


def _outline_level(ppr) -> int | None:
    if ppr is None:
        return None
    el = ppr.find(qn("w:outlineLvl"))
    if el is None:
        return None
    try:
        return int(el.get(qn("w:val")))
    except (TypeError, ValueError):
        return None


class DocxParser:
    extensions = (".docx",)
    file_type = "docx"

    def parse(self, path: Path) -> Iterator[Block]:
        with open(path, "rb") as f:
            if f.read(8) == OLE_MAGIC:
                raise EncryptedFileError("文件受密码保护，或是改了扩展名的旧版 .doc")
        try:
            document = docx.Document(str(path))
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"无法打开 DOCX：{e}") from e

        headings: list[tuple[int, str]] = []
        ordinal = 0
        for item in document.iter_inner_content():
            ordinal += 1
            if isinstance(item, Paragraph):
                text = clean_text(item.text)
                if not text:
                    continue
                level = heading_level(item)
                if level is not None:
                    headings = [h for h in headings if h[0] < level] + [(level, text)]
                    continue
                yield Block(text=text, location=self._loc(headings, ordinal))
            elif isinstance(item, Table):
                for row in item.rows:
                    cells: list[str] = []
                    for cell in row.cells:
                        t = clean_text(cell.text)
                        if t and (not cells or cells[-1] != t):  # 合并单元格会重复出现
                            cells.append(t)
                    if cells:
                        yield Block(text=" | ".join(cells), location=self._loc(headings, ordinal))

    @staticmethod
    def _loc(headings: list[tuple[int, str]], ordinal: int) -> Location:
        path = " > ".join(h[1] for h in headings) or None
        return Location(heading_path=path, para_start=ordinal, para_end=ordinal)
