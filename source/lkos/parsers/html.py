"""网页：.html / .htm / .xhtml（保存的网页、导出的笔记）与 Safari 的 .webarchive。

按标题（h1–h6）记录标题路径，按段落、列表项、表格行分段；去掉脚本、样式、导航栏等不是正文的部分。
电子书（EPUB）的章节也是 XHTML，复用这里的 html_blocks。
"""

from __future__ import annotations

import plistlib
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

from lkos.errors import ParseError
from lkos.parsers.base import decode_text
from lkos.types import Block, Location

SKIP = {"script", "style", "noscript", "template", "svg", "nav", "iframe", "select", "button", "math"}
BLOCK = {"p", "div", "li", "tr", "br", "pre", "blockquote", "section", "article", "dd", "dt", "caption",
         "figcaption", "table", "ul", "ol", "dl", "hr", "main", "aside", "header", "footer", "form", "address",
         "summary", "details"}
CELL = {"td", "th"}
VOID = {"br", "hr", "img", "meta", "link", "input", "area", "base", "col", "embed", "source", "track", "wbr", "param"}
_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.I)


class _Blocks(HTMLParser):
    def __init__(self, base: list[str] | None = None):
        super().__init__(convert_charrefs=True)
        self.base = list(base or [])
        self.headings: list[tuple[int, str]] = []
        self.blocks: list[Block] = []
        self.buf: list[str] = []
        self.skip = 0
        self.heading: tuple[int, list[str]] | None = None
        self.pre = 0
        self.in_title = False
        self.title_parts: list[str] = []

    def _path(self) -> str | None:
        return " > ".join(self.base + [h[1] for h in self.headings]) or None

    def flush(self) -> None:
        text = "".join(self.buf)
        text = text if self.pre else re.sub(r"[ \t\r\n\xa0]+", " ", text)
        text = text.strip()
        if text:
            self.blocks.append(Block(text=text, location=Location(heading_path=self._path())))
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
            return
        if tag in SKIP:
            if tag not in VOID:
                self.skip += 1
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.flush()
            self.heading = (int(tag[1]), [])
        elif tag in BLOCK:
            if tag == "br" and self.heading is None:
                self.buf.append("\n" if self.pre else " ")
                if not self.pre:
                    self.flush()
                return
            self.flush()
            if tag == "pre":
                self.pre += 1
        elif tag in CELL:
            self.buf.append(" | ")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt and alt.strip():
                (self.heading[1] if self.heading else self.buf).append(f" {alt.strip()} ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
            return
        if tag in SKIP:
            if tag not in VOID and self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag) and self.heading:
            level, parts = self.heading
            title = re.sub(r"\s+", " ", "".join(parts)).strip()
            self.heading = None
            if title:
                self.headings = [h for h in self.headings if h[0] < level] + [(level, title)]
                self.blocks.append(Block(text=title, location=Location(heading_path=self._path())))
        elif tag in BLOCK:
            self.flush()
            if tag == "pre" and self.pre:
                self.pre -= 1

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip:
            return
        if self.heading is not None:
            self.heading[1].append(data)
        else:
            self.buf.append(data)


def html_blocks(text: str, base: list[str] | None = None, with_title: bool = True) -> list[Block]:
    p = _Blocks(base)
    p.feed(text)
    p.close()
    p.flush()
    blocks = p.blocks
    title = re.sub(r"\s+", " ", "".join(p.title_parts)).strip()
    if with_title and title and not any(b.text == title for b in blocks[:3]):
        blocks.insert(0, Block(text=title, location=Location(heading_path=" > ".join(base or []) or None)))
    return blocks


def decode_html(raw: bytes) -> str:
    m = _CHARSET.search(raw[:4096])
    if m:
        enc = m.group(1).decode("ascii", "ignore").lower()
        if enc in ("gb2312", "gbk"):
            enc = "gb18030"
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            pass
    return decode_text(raw)


class HtmlParser:
    extensions = (".html", ".htm", ".xhtml", ".webarchive")
    file_type = "html"
    label = "网页"

    def parse(self, path: Path) -> Iterator[Block]:
        raw = Path(path).read_bytes()
        if Path(path).suffix.lower() == ".webarchive":
            try:
                arc = plistlib.loads(raw)
                res = arc["WebMainResource"]
                raw = res["WebResourceData"]
                enc = res.get("WebResourceTextEncodingName")
            except (plistlib.InvalidFileException, KeyError, TypeError, ValueError) as e:
                raise ParseError(f"无法读取 Safari 网页存档：{e}") from e
            if enc:
                try:
                    yield from html_blocks(raw.decode(enc))
                    return
                except (LookupError, UnicodeDecodeError):
                    pass
        yield from html_blocks(decode_html(raw))
