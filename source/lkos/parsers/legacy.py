"""旧版 Office 格式。

- .doc：用 macOS 自带的 textutil 转成 docx 再解析（不需要安装 Office / LibreOffice），引用定位与 DOCX 相同；
  其他系统上没有 textutil，跳过并提示另存为 .docx。
- .ppt：没有可靠的离线转换工具（决定不装 LibreOffice），跳过并在资料库里提示用 WPS / Keynote 另存为 .pptx。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator

from lkos.errors import ParseError, UnsupportedFileError
from lkos.types import Block


class DocParser:
    extensions = (".doc",)
    file_type = "doc"

    def parse(self, path: Path) -> Iterator[Block]:
        if sys.platform != "darwin":
            raise UnsupportedFileError("旧版 .doc 只能在 macOS 上自动转换；请用 WPS / Word 另存为 .docx")
        from lkos.parsers.docx import DocxParser

        with tempfile.TemporaryDirectory(prefix="lkos-doc-") as tmp:
            out = Path(tmp) / "converted.docx"
            try:
                r = subprocess.run(["textutil", "-convert", "docx", "-output", str(out), str(path)],
                                   capture_output=True, text=True, timeout=180)
            except (OSError, subprocess.SubprocessError) as e:
                raise ParseError(f"textutil 转换失败：{e}") from e
            if r.returncode != 0 or not out.exists():
                msg = (r.stderr or r.stdout or "").strip()[:200]
                raise ParseError(f"textutil 无法转换（文件可能受密码保护或已损坏）：{msg}")
            yield from DocxParser().parse(out)


class PptParser:
    extensions = (".ppt",)
    file_type = "ppt"

    def parse(self, path: Path) -> Iterator[Block]:
        raise UnsupportedFileError("旧版 .ppt：请用 WPS 或 Keynote 另存为 .pptx，下次更新时会自动索引")
        yield  # pragma: no cover - 让它成为生成器
