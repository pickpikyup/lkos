"""PPTX 解析：逐页幻灯片抽取标题、文本框、表格、组合形状里的文字，以及演讲者备注。

引用定位到“幻灯片 N”（存在 Location.page 里），标题路径为该页标题；备注的标题路径为“<标题> > 备注”。
同一页内按形状在页面上的位置（从上到下、从左到右）排序，接近阅读顺序。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from lkos.errors import EncryptedFileError, ParseError
from lkos.parsers.base import clean_text
from lkos.types import Block, Location

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # 加密的 pptx 或改了扩展名的旧版 .ppt


def _shape_texts(shapes) -> Iterator[str]:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    ordered = sorted(shapes, key=lambda s: ((s.top or 0), (s.left or 0)))
    for shape in ordered:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _shape_texts(shape.shapes)
            continue
        if getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                cells: list[str] = []
                for cell in row.cells:
                    t = clean_text(cell.text)
                    if t and (not cells or cells[-1] != t):
                        cells.append(t)
                if cells:
                    yield " | ".join(cells)
            continue
        if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                t = clean_text(para.text)
                if t:
                    yield t


class PptxParser:
    extensions = (".pptx",)
    file_type = "pptx"

    def parse(self, path: Path) -> Iterator[Block]:
        with open(path, "rb") as f:
            if f.read(8) == OLE_MAGIC:
                raise EncryptedFileError("文件受密码保护，或是改了扩展名的旧版 .ppt")
        try:
            from pptx import Presentation

            prs = Presentation(str(path))
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"无法打开 PPTX：{e}") from e

        for no, slide in enumerate(prs.slides, start=1):
            title_shape = slide.shapes.title
            title = clean_text(title_shape.text) if title_shape is not None and title_shape.has_text_frame else ""
            heading = title or None
            loc = Location(page=no, heading_path=heading)
            if title:
                yield Block(text=title, location=loc)
            for text in _shape_texts([s for s in slide.shapes if s is not title_shape
                                      and (title_shape is None or s.shape_id != title_shape.shape_id)]):
                yield Block(text=text, location=loc)
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame
                if notes is not None:
                    note_loc = Location(page=no, heading_path=f"{title} > 备注" if title else "备注")
                    for para in notes.paragraphs:
                        t = clean_text(para.text)
                        if t:
                            yield Block(text=t, location=note_loc)
