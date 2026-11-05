"""PDF 解析：按页抽取文本块，定位到页码。

扫描页（没有文字层）默认跳过；传入识别引擎（ocr）时，把这些页面渲染成图片做文字识别（见 parsers/ocr.py）。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

try:
    import pymupdf as fitz
except ImportError:  # 旧版本只有 fitz 包名
    import fitz

from lkos.errors import EncryptedFileError, ParseError
from lkos.parsers.base import clean_text
from lkos.parsers.ocr import DPI, MIN_PAGE_CHARS
from lkos.types import Block, Location

try:
    fitz.TOOLS.mupdf_display_errors(False)
except Exception:  # noqa: BLE001 - 旧版本没有此开关
    pass


class PdfParser:
    extensions = (".pdf",)
    file_type = "pdf"

    def parse(self, path: Path, ocr: Callable[[bytes], str] | None = None,
              before_ocr: Callable[[], None] | None = None) -> Iterator[Block]:
        """ocr：识别引擎（PNG 字节 → 文字），文字层少于 MIN_PAGE_CHARS 字的页面用它识别；
        before_ocr：每识别一页之前调用（闲时闸门：正在问答时先等一等）。"""
        try:
            doc = fitz.open(str(path))
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"无法打开 PDF：{e}") from e
        try:
            if doc.needs_pass:
                raise EncryptedFileError("PDF 受密码保护")
            for index, page in enumerate(doc):
                blocks = []
                for b in page.get_text("blocks", sort=True):
                    if b[6] != 0:  # 0 = 文本块，1 = 图片
                        continue
                    text = clean_text(b[4])
                    if text:
                        blocks.append(Block(text=text, location=Location(page=index + 1)))
                if ocr is not None and sum(len(b.text) for b in blocks) < MIN_PAGE_CHARS:
                    if before_ocr is not None:
                        before_ocr()
                    zoom = DPI / 72
                    png = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")
                    text = clean_text(ocr(png) or "")
                    if len(text) > sum(len(b.text) for b in blocks):
                        blocks = [Block(text=text, location=Location(page=index + 1))]
                yield from blocks
        finally:
            doc.close()
