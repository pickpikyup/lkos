"""RTF 富文本（文本编辑 TextEdit、WordPad、很多软件导出的 .rtf）。

macOS 上用系统自带的 textutil 转成 docx 再解析（保留标题、段落，定位与 Word 相同）；
其他系统或转换失败时，用内置的简易解析取出文字（支持 \\uN 与 \\'hh 编码，含 GBK 中文）。
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator

from lkos.errors import ParseError
from lkos.logs import get_logger
from lkos.types import Block, Location

log = get_logger("parsers")

# 这些组是格式表、图片、元数据，不是正文
SKIP_DEST = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "footer", "headerl", "headerr",
             "footerl", "footerr", "footnote", "listtable", "listoverridetable", "rsidtbl", "generator", "xmlnstbl",
             "themedata", "colorschememapping", "latentstyles", "datastore", "filetbl", "revtbl", "pgdsctbl",
             "bkmkstart", "bkmkend", "field", "fldinst", "nonshppict", "shppict", "expandedcolortbl", "mmathPr"}
_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}\r\n]+)|[\r\n]+", re.S)
CODEPAGES = {936: "gb18030", 950: "big5", 932: "cp932", 949: "cp949", 1252: "cp1252", 1250: "cp1250",
             10000: "mac_roman", 65001: "utf-8"}


def rtf_to_text(data: str) -> str:
    out: list[str] = []
    pending = bytearray()
    enc = "cp1252"
    stack: list[tuple[bool, int]] = []
    skip, uc, ucskip = False, 1, 0
    star = False

    def flush_bytes():
        if pending:
            out.append(pending.decode(enc, errors="replace"))
            pending.clear()

    for m in _TOKEN.finditer(data):
        word, arg, hexv, sym, brace, text = m.groups()
        if hexv is not None:
            if ucskip:
                ucskip -= 1
                continue
            if not skip:
                pending.append(int(hexv, 16))
            continue
        flush_bytes()
        if brace == "{":
            stack.append((skip, uc))
            star = False
        elif brace == "}":
            if stack:
                skip, uc = stack.pop()
        elif word is not None:
            if ucskip and word not in ("u",):
                ucskip = 0
            if word in SKIP_DEST or (star and not skip):
                skip = True
                star = False
                continue
            if skip:
                continue
            if word == "ansicpg" and arg:
                enc = CODEPAGES.get(int(arg), f"cp{arg}")
                try:
                    "".encode(enc)
                except LookupError:
                    enc = "cp1252"
            elif word == "uc" and arg:
                uc = int(arg)
            elif word == "u" and arg:
                n = int(arg)
                out.append(chr(n + 65536 if n < 0 else n))
                ucskip = uc
            elif word in ("par", "line", "sect", "page", "row"):
                out.append("\n")
            elif word in ("tab", "cell"):
                out.append("\t")
            elif word == "emdash":
                out.append("—")
            elif word == "endash":
                out.append("–")
            elif word in ("lquote", "rquote"):
                out.append("'")
            elif word in ("ldblquote", "rdblquote"):
                out.append('"')
            elif word == "bullet":
                out.append("•")
        elif sym is not None:
            if sym == "*":
                star = True
                continue
            if skip:
                continue
            if sym in "\\{}":
                out.append(sym)
            elif sym == "~":
                out.append("\xa0")
            elif sym in "\n\r":
                out.append("\n")
        elif text is not None:
            if skip:
                continue
            if ucskip:
                drop = min(ucskip, len(text))
                text = text[drop:]
                ucskip -= drop
            out.append(text)
    flush_bytes()
    return "".join(out)


def _paragraphs(text: str) -> Iterator[Block]:
    n = 0
    for para in text.split("\n"):
        n += 1
        para = re.sub(r"[ \t\xa0]+", " ", para).strip()
        if para:
            yield Block(text=para, location=Location(para_start=n, para_end=n))


class RtfParser:
    extensions = (".rtf",)
    file_type = "rtf"
    label = "RTF"

    def parse(self, path: Path) -> Iterator[Block]:
        if sys.platform == "darwin":
            blocks = self._textutil(path)
            if blocks is not None:
                yield from blocks
                return
        raw = Path(path).read_bytes()
        if not raw.lstrip().startswith(b"{\\rtf"):
            raise ParseError("不是有效的 RTF 文件")
        yield from _paragraphs(rtf_to_text(raw.decode("latin-1")))

    @staticmethod
    def _textutil(path: Path) -> list[Block] | None:
        from lkos.parsers.docx import DocxParser

        with tempfile.TemporaryDirectory(prefix="lkos-rtf-") as tmp:
            out = Path(tmp) / "converted.docx"
            try:
                r = subprocess.run(["textutil", "-convert", "docx", "-output", str(out), str(path)],
                                   capture_output=True, text=True, timeout=180)
                if r.returncode == 0 and out.exists():
                    return list(DocxParser().parse(out))
            except (OSError, subprocess.SubprocessError, ParseError) as e:
                log.info("textutil 转换 RTF 失败，改用内置解析：%s", e)
        return None
