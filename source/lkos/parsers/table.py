"""表格：Excel（.xlsx / .xlsm）与 CSV / TSV。

- 每个工作表的第一行若像表头（都是文字），之后每一行写成“列名：值；列名：值”，单独检索到一行时也看得懂；
- 引用定位到“工作表名 · 行号”；日期按单元格格式转成 2026-11-08；公式取 Excel 保存时算好的值；
- .xlsx 直接读文件里的 XML（不需要另装库）；每个工作表最多索引 MAX_ROWS 行（更大的多半是数据导出，检索意义不大）。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator
from xml.etree import ElementTree as ET

from lkos.errors import EncryptedFileError, ParseError
from lkos.logs import get_logger
from lkos.parsers.base import decode_text
from lkos.types import Block, Location

log = get_logger("parsers")
MAX_ROWS = 20000
MAX_CELL = 1000
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_NUMERIC = re.compile(r"^[\s\d.,%+\-¥$€£/:年月日]*$")


def _fmt_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.10g}"
    s = str(v).replace("\r", " ").replace("\n", " ").strip()
    return s[:MAX_CELL]


def table_blocks(rows: Iterable[tuple[int, list[str]]], sheet: str | None = None, name: str = "表格") -> Iterator[Block]:
    """rows：(行号, 单元格文字列表)。有表头时每行写成“列名：值”。"""
    header: list[str] | None = None
    first = True
    n = 0
    for rowno, cells in rows:
        cells = [_fmt_cell(c) for c in cells]
        while cells and not cells[-1]:
            cells.pop()
        if not any(cells):
            continue
        if first:
            first = False
            filled = [c for c in cells if c]
            if len(filled) >= 2 and all(not _NUMERIC.match(c) for c in filled):
                header = cells
                yield Block(text=f"{sheet or name}的列：" + "、".join(filled),
                            location=Location(heading_path=sheet, line_start=rowno, line_end=rowno))
                continue
        n += 1
        if n > MAX_ROWS:
            log.info("%s 超过 %d 行，只索引前 %d 行", sheet or name, MAX_ROWS, MAX_ROWS)
            break
        if header:
            parts = []
            for i, c in enumerate(cells):
                if not c:
                    continue
                h = header[i] if i < len(header) and header[i] else f"第{i + 1}列"
                parts.append(f"{h}：{c}")
            text = "；".join(parts)
        else:
            text = " | ".join(cells)
        yield Block(text=text, location=Location(heading_path=sheet, line_start=rowno, line_end=rowno))


# ---------------- CSV ----------------
class CsvParser:
    extensions = (".csv", ".tsv")
    file_type = "csv"
    label = "表格"

    def parse(self, path: Path) -> Iterator[Block]:
        text = decode_text(Path(path).read_bytes())
        if Path(path).suffix.lower() == ".tsv":
            delim = "\t"
        else:
            try:
                delim = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|").delimiter
            except csv.Error:
                delim = ","
        reader = csv.reader(io.StringIO(text), delimiter=delim)
        try:
            yield from table_blocks(((i, row) for i, row in enumerate(reader, 1)), name=Path(path).stem)
        except csv.Error as e:
            raise ParseError(f"CSV 格式不对：{e}") from e


# ---------------- XLSX ----------------
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
_M = "{%s}" % NS["m"]
# 内置的日期 / 时间格式编号（含中文版 Excel 的 27–36、50–58）
DATE_FMT_IDS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
_COL = re.compile(r"([A-Z]+)(\d+)")


def _col_index(ref: str) -> int:
    m = _COL.match(ref or "")
    if not m:
        return -1
    n = 0
    for ch in m.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _is_date_format(code: str) -> bool:
    code = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", code or "").lower()
    return bool(re.search(r"[ymdh]", code)) and "general" not in code


def _date(serial: float, date1904: bool) -> str:
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    try:
        d = base + timedelta(days=serial)
    except OverflowError:
        return _fmt_cell(serial)
    if serial == int(serial):
        return d.strftime("%Y-%m-%d")
    if serial < 1:
        return d.strftime("%H:%M")
    return d.strftime("%Y-%m-%d %H:%M")


class XlsxParser:
    extensions = (".xlsx", ".xlsm")
    file_type = "xlsx"
    label = "表格"

    def parse(self, path: Path) -> Iterator[Block]:
        with open(path, "rb") as f:
            if f.read(8) == OLE_MAGIC:
                raise EncryptedFileError("表格受密码保护（或是改了扩展名的旧版 .xls）")
        try:
            z = zipfile.ZipFile(path)
        except zipfile.BadZipFile as e:
            raise ParseError(f"无法打开 Excel 文件：{e}") from e
        with z:
            names = set(z.namelist())
            shared = self._shared_strings(z) if "xl/sharedStrings.xml" in names else []
            date_styles = self._date_styles(z) if "xl/styles.xml" in names else set()
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            pr = wb.find(f"{_M}workbookPr")
            date1904 = pr is not None and pr.get("date1904") in ("1", "true")
            rels = {}
            if "xl/_rels/workbook.xml.rels" in names:
                for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                    rels[r.get("Id")] = r.get("Target")
            for sh in wb.iter(f"{_M}sheet"):
                target = rels.get(sh.get("{%s}id" % NS["r"]))
                if not target:
                    continue
                target = target.lstrip("/")
                member = target if target.startswith("xl/") else str(PurePosixPath("xl") / target)
                if member not in names:
                    continue
                name = sh.get("name") or member
                rows = self._rows(z, member, shared, date_styles, date1904)
                yield from table_blocks(rows, sheet=name)

    @staticmethod
    def _shared_strings(z: zipfile.ZipFile) -> list[str]:
        out = []
        with z.open("xl/sharedStrings.xml") as f:
            for _, el in ET.iterparse(f):
                if el.tag == f"{_M}si":
                    # 只取正文（t），不取拼音注音（rPh 里的 t）
                    parts = [t.text or "" for t in el.iter(f"{_M}t") if t not in _rph_texts(el)]
                    out.append("".join(parts))
                    el.clear()
        return out

    @staticmethod
    def _date_styles(z: zipfile.ZipFile) -> set[int]:
        st = ET.fromstring(z.read("xl/styles.xml"))
        custom = {}
        nf = st.find(f"{_M}numFmts")
        if nf is not None:
            for x in nf:
                try:
                    custom[int(x.get("numFmtId"))] = x.get("formatCode") or ""
                except (TypeError, ValueError):
                    pass
        out = set()
        xfs = st.find(f"{_M}cellXfs")
        for i, xf in enumerate(xfs if xfs is not None else []):
            try:
                fid = int(xf.get("numFmtId") or 0)
            except ValueError:
                continue
            if fid in DATE_FMT_IDS or (fid in custom and _is_date_format(custom[fid])):
                out.add(i)
        return out

    @staticmethod
    def _rows(z, member, shared, date_styles, date1904) -> Iterator[tuple[int, list[str]]]:
        with z.open(member) as f:
            auto = 0
            for _, el in ET.iterparse(f):
                if el.tag != f"{_M}row":
                    continue
                auto += 1
                try:
                    rowno = int(el.get("r") or auto)
                except ValueError:
                    rowno = auto
                auto = rowno
                cells: dict[int, str] = {}
                nxt = 0
                for c in el.iter(f"{_M}c"):
                    idx = _col_index(c.get("r") or "")
                    if idx < 0:
                        idx = nxt
                    nxt = idx + 1
                    if idx > 500:
                        continue
                    t = c.get("t")
                    v = c.find(f"{_M}v")
                    val = None
                    if t == "s" and v is not None and v.text:
                        try:
                            val = shared[int(v.text)]
                        except (ValueError, IndexError):
                            val = None
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{_M}t"))
                    elif t == "b" and v is not None:
                        val = "是" if v.text == "1" else "否"
                    elif t == "e":
                        val = None
                    elif v is not None and v.text is not None:
                        if t in ("str", "d"):
                            val = v.text
                        else:
                            try:
                                num = float(v.text)
                            except ValueError:
                                val = v.text
                            else:
                                try:
                                    style = int(c.get("s") or 0)
                                except ValueError:
                                    style = 0
                                val = _date(num, date1904) if style in date_styles else _fmt_cell(num)
                    if val not in (None, ""):
                        cells[idx] = val
                el.clear()
                if cells:
                    yield rowno, [cells.get(i, "") for i in range(max(cells) + 1)]


def _rph_texts(si) -> set:
    out = set()
    for rph in si.iter(f"{_M}rPh"):
        out.update(rph.iter(f"{_M}t"))
    return out
