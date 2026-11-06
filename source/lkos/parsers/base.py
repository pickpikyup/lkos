"""解析器协议与按扩展名选择解析器。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Protocol

from lkos.errors import Cancelled, EncryptedFileError, ParseError
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
    from lkos.parsers.legacy import DocParser, PptParser
    from lkos.parsers.markdown import MarkdownParser
    from lkos.parsers.pdf import PdfParser
    from lkos.parsers.pptx import PptxParser

    return [PdfParser(), DocxParser(), PptxParser(), MarkdownParser(), DocParser(), PptParser()]


def file_type_groups(allowed: list[str] | None = None) -> list[dict]:
    """界面上可选的文件类型（范围筛选、文件夹设置里的“不索引”），直接由解析器注册表生成：
    以后新增一种格式，只要在 _registry() 里加上解析器（写好 label），界面就会自动出现。

    返回 [{"key", "label", "types"（files.file_type 值）, "exts"（扩展名，不带点）, "indexable"，
    "skipped_exts"（能识别但不解析、只提示的扩展名，如 .ppt）}]，
    同一个 label 的解析器合成一组（如 Word = .docx + .doc），按注册顺序排列。"""
    groups: dict[str, dict] = {}
    for p in _registry():
        exts = [e for e in p.extensions if allowed is None or e in allowed]
        if not exts:
            continue
        label = getattr(p, "label", None) or p.file_type.upper()
        g = groups.setdefault(label, {"key": label.lower(), "label": label, "types": [], "exts": [], "indexable": False,
                                      "skipped_exts": []})
        g["types"].append(p.file_type)
        g["exts"] += [e.lstrip(".") for e in exts]
        if getattr(p, "supported", True):
            g["indexable"] = True
        else:
            g["skipped_exts"] += [e.lstrip(".") for e in exts]
    return list(groups.values())


def parser_for(path: Path, allowed: list[str] | None = None) -> Parser | None:
    ext = path.suffix.lower()
    if allowed is not None and ext not in allowed:
        return None
    for p in _registry():
        if ext in p.extensions:
            return p
    return None


def parse_file(parser: Parser, path: Path, **options) -> list[Block]:
    """统一异常：未预期的异常包装为 ParseError，便于 Indexer 记录失败原因。options 传给解析器（如 PDF 的 ocr）。"""
    try:
        return [b for b in parser.parse(path, **options) if b.text.strip()]
    except (EncryptedFileError, ParseError, Cancelled):  # 含 UnsupportedFileError；Cancelled 来自闲时闸门
        raise
    except Exception as e:  # noqa: BLE001 - 第三方解析库抛出的异常种类不可控
        raise ParseError(f"{type(e).__name__}: {e}") from e
