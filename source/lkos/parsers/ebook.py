"""电子书：EPUB（按书脊 spine 的顺序读每一章的 XHTML，标题路径来自章节标题）。

加了 DRM 的电子书（如从商店下载的 .epub 有 META-INF/encryption.xml 且正文被加密）无法读取，跳过并提示。
.mobi / .azw3 等格式没有可靠的离线解析方式，暂不支持（资料体检里会列出）。
"""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

from lkos.errors import EncryptedFileError, ParseError
from lkos.parsers.html import decode_html, html_blocks
from lkos.types import Block, Location

HTML_TYPES = {"application/xhtml+xml", "text/html", "application/x-dtbook+xml"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class EpubParser:
    extensions = (".epub",)
    file_type = "epub"
    label = "电子书"

    def parse(self, path: Path) -> Iterator[Block]:
        try:
            z = zipfile.ZipFile(path)
        except zipfile.BadZipFile as e:
            raise ParseError(f"无法打开电子书：{e}") from e
        with z:
            names = set(z.namelist())
            try:
                container = ET.fromstring(z.read("META-INF/container.xml"))
                opf_path = next(el.get("full-path") for el in container.iter() if _local(el.tag) == "rootfile")
                opf = ET.fromstring(z.read(opf_path))
            except (KeyError, StopIteration, ET.ParseError) as e:
                raise ParseError(f"电子书结构不完整：{e}") from e
            base = posixpath.dirname(opf_path)
            encrypted = set()
            if "META-INF/encryption.xml" in names:
                try:
                    enc = ET.fromstring(z.read("META-INF/encryption.xml"))
                    encrypted = {el.get("URI") for el in enc.iter() if _local(el.tag) == "CipherReference"}
                except ET.ParseError:
                    pass
            manifest, spine, title = {}, [], None
            for el in opf.iter():
                t = _local(el.tag)
                if t == "item":
                    manifest[el.get("id")] = (el.get("href") or "", el.get("media-type") or "")
                elif t == "itemref" and el.get("linear") != "no":
                    spine.append(el.get("idref"))
                elif t == "title" and title is None and (el.text or "").strip():
                    title = el.text.strip()
            if title:
                yield Block(text=title, location=Location())
            done = 0
            skipped = 0
            for idref in spine:
                href, mtype = manifest.get(idref, ("", ""))
                if not href or (mtype and mtype not in HTML_TYPES):
                    continue
                member = posixpath.normpath(posixpath.join(base, href.split("#", 1)[0]))
                if member in encrypted:
                    skipped += 1
                    continue
                try:
                    raw = z.read(member)
                except KeyError:
                    continue
                for b in html_blocks(decode_html(raw), with_title=False):
                    yield b
                done += 1
            if not done and skipped:
                raise EncryptedFileError("电子书有版权保护（DRM），无法读取")
