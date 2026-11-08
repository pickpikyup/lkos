"""OpenDocument（LibreOffice / WPS 可存的开放格式）：.odt 文档、.odp 演示文稿、.ods 表格。

都是 zip 里的 content.xml：文档按标题与段落（定位到段落序号），演示文稿按页（幻灯片 n），表格按工作表与行。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

from lkos.errors import EncryptedFileError, ParseError
from lkos.parsers.base import clean_text
from lkos.parsers.table import table_blocks
from lkos.types import Block, Location

TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
T, TB, D, O = (f"{{{x}}}" for x in (TEXT, TABLE, DRAW, OFFICE))
MAX_REPEAT = 200


def _text(el) -> str:
    parts: list[str] = []

    def walk(e):
        if e.text:
            parts.append(e.text)
        for c in e:
            tag = c.tag
            if tag == f"{T}s":
                parts.append(" " * int(c.get(f"{T}c") or 1))
            elif tag == f"{T}tab":
                parts.append("\t")
            elif tag == f"{T}line-break":
                parts.append("\n")
            elif tag in (f"{T}note", f"{O}annotation"):
                pass
            else:
                walk(c)
            if c.tail:
                parts.append(c.tail)
    walk(el)
    return clean_text("".join(parts))


def _content(path: Path):
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ParseError(f"无法打开文件：{e}") from e
    with z:
        try:
            manifest = z.read("META-INF/manifest.xml")
        except KeyError:
            manifest = b""
        if b"encryption-data" in manifest:
            raise EncryptedFileError("文件受密码保护")
        try:
            return ET.fromstring(z.read("content.xml"))
        except (KeyError, ET.ParseError) as e:
            raise ParseError(f"文件内容不完整：{e}") from e


class OdtParser:
    extensions = (".odt",)
    file_type = "odt"
    label = "Word"

    def parse(self, path: Path) -> Iterator[Block]:
        root = _content(path)
        body = root.find(f"{O}body")
        if body is None:
            return
        headings: list[tuple[int, str]] = []
        n = 0
        for el in body.iter():
            if el.tag == f"{T}h":
                n += 1
                title = _text(el)
                if title:
                    level = int(el.get(f"{T}outline-level") or 1)
                    headings = [h for h in headings if h[0] < level] + [(level, title)]
            elif el.tag == f"{T}p":
                n += 1
                text = _text(el)
                if text:
                    hp = " > ".join(h[1] for h in headings) or None
                    yield Block(text=text, location=Location(heading_path=hp, para_start=n, para_end=n))


class OdpParser:
    extensions = (".odp",)
    file_type = "odp"
    label = "PowerPoint"

    def parse(self, path: Path) -> Iterator[Block]:
        root = _content(path)
        for i, page in enumerate(root.iter(f"{D}page"), 1):
            title = page.get(f"{D}name")
            for p in page.iter(f"{T}p"):
                text = _text(p)
                if text:
                    yield Block(text=text, location=Location(page=i, heading_path=None if not title or
                                                             title.startswith("page") else title))


class OdsParser:
    extensions = (".ods",)
    file_type = "ods"
    label = "表格"

    def parse(self, path: Path) -> Iterator[Block]:
        root = _content(path)
        for table in root.iter(f"{TB}table"):
            yield from table_blocks(self._rows(table), sheet=table.get(f"{TB}name"))

    @staticmethod
    def _rows(table) -> Iterator[tuple[int, list[str]]]:
        rowno = 0
        for row in table.iter(f"{TB}table-row"):
            rep = int(row.get(f"{TB}number-rows-repeated") or 1)
            cells: list[str] = []
            for c in row:
                if c.tag not in (f"{TB}table-cell", f"{TB}covered-table-cell"):
                    continue
                crep = min(int(c.get(f"{TB}number-columns-repeated") or 1), MAX_REPEAT)
                val = " ".join(_text(p) for p in c.iter(f"{T}p")).strip()
                cells += [val] * crep
            rowno += 1
            if any(cells):
                for k in range(min(rep, MAX_REPEAT)):
                    yield rowno + k, cells
            rowno += rep - 1
