"""解析器协议与按扩展名选择解析器。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Protocol

from lkos.errors import EncryptedFileError, ParseError
from lkos.types import Block


class Parser(Protocol):
    extensions: tuple[str, ...]
    file_type: str

    def parse(self, path: Path) -> Iterator[Block]:
        """失败抛 ParseError；加密文件抛 EncryptedFileError。"""
        ...


_CJK = r"㐀-䶿一-鿿豈-﫿　-〿＀-￯"


def clean_text(text: str) -> str:
    """合并 PDF/DOCX 中的硬换行：中文之间直接相连，其他情况补一个空格。"""
    text = text.replace(" ", " ").replace("\r", "")
    text = re.sub(rf"(?<=[{_CJK}])\n(?=[{_CJK}])", "", text)
    text = re.sub(r"-\n(?=[a-zà-ÿ])", "", text)  # 英文/法文断词连字符
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _registry() -> list[Parser]:
    from lkos.parsers.docx import DocxParser
    from lkos.parsers.markdown import MarkdownParser
    from lkos.parsers.pdf import PdfParser

    return [PdfParser(), DocxParser(), MarkdownParser()]


def parser_for(path: Path, allowed: list[str] | None = None) -> Parser | None:
    ext = path.suffix.lower()
    if allowed is not None and ext not in allowed:
        return None
    for p in _registry():
        if ext in p.extensions:
            return p
    return None


def parse_file(parser: Parser, path: Path) -> list[Block]:
    """统一异常：未预期的异常包装为 ParseError，便于 Indexer 记录失败原因。"""
    try:
        return [b for b in parser.parse(path) if b.text.strip()]
    except (EncryptedFileError, ParseError):
        raise
    except Exception as e:  # noqa: BLE001 - 第三方解析库抛出的异常种类不可控
        raise ParseError(f"{type(e).__name__}: {e}") from e
