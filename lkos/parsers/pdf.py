"""PDF 解析：按页抽取文本块，定位到页码。扫描页（无文本层）POC 阶段直接跳过。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

try:
    import pymupdf as fitz
except ImportError:  # 旧版本只有 fitz 包名
    import fitz

from lkos.errors import EncryptedFileError, ParseError
from lkos.parsers.base import clean_text
from lkos.types import Block, Location

try:
    fitz.TOOLS.mupdf_display_errors(False)
except Exception:  # noqa: BLE001 - 旧版本没有此开关
    pass


class PdfParser:
    extensions = (".pdf",)
    file_type = "pdf"

    def parse(self, path: Path) -> Iterator[Block]:
        try:
            doc = fitz.open(str(path))
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"无法打开 PDF：{e}") from e
        try:
            if doc.needs_pass:
                raise EncryptedFileError("PDF 受密码保护")
            for index, page in enumerate(doc):
                for b in page.get_text("blocks", sort=True):
                    if b[6] != 0:  # 0 = 文本块，1 = 图片
                        continue
                    text = clean_text(b[4])
                    if text:
                        yield Block(text=text, location=Location(page=index + 1))
        finally:
            doc.close()
