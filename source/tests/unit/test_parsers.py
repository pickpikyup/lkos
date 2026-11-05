from pathlib import Path

import pytest

from lkos.errors import EncryptedFileError, ParseError
from lkos.parsers import parse_file, parser_for
from lkos.parsers.base import clean_text
from lkos.parsers.markdown import parse_markdown_text
from lkos.types import Location
from tests.samples import EN_POLICY_MD, ZH_MINUTES_PAGES, ZH_PLAN, make_docx, make_pdf


def parse(path: Path):
    return parse_file(parser_for(path), path)


# ---------- 选择解析器 ----------
@pytest.mark.parametrize("name,ptype", [("a.pdf", "pdf"), ("a.PDF", "pdf"), ("a.docx", "docx"), ("a.md", "md")])
def test_parser_for_extension(name, ptype):
    assert parser_for(Path(name)).file_type == ptype


def test_parser_for_unsupported_or_not_allowed():
    assert parser_for(Path("a.txt")) is None
    assert parser_for(Path("a.key")) is None
    assert parser_for(Path("a.pdf"), allowed=[".md"]) is None


# ---------- 文本清理 ----------
def test_clean_text_joins_cjk_and_latin_lines():
    assert clean_text("二期预算\n上限") == "二期预算上限"
    assert clean_text("annual\nbudget") == "annual budget"
    assert clean_text("bud-\nget") == "budget"
    assert clean_text("  a  b \r\n") == "a b"


# ---------- PDF ----------
def test_pdf_blocks_carry_page_numbers(tmp_path):
    p = make_pdf(tmp_path / "m.pdf", ZH_MINUTES_PAGES)
    blocks = parse(p)
    pages = {b.location.page for b in blocks}
    assert pages == {1, 2}
    page2 = " ".join(b.text for b in blocks if b.location.page == 2)
    assert "确认二期预算上限" in page2


def test_pdf_blank_page_yields_nothing(tmp_path):
    p = make_pdf(tmp_path / "blank.pdf", [""])
    assert parse(p) == []


def test_pdf_encrypted_raises(tmp_path):
    p = make_pdf(tmp_path / "e.pdf", ["secret"], encrypt_password="pw")
    with pytest.raises(EncryptedFileError):
        parse(p)


def test_pdf_corrupt_raises_parse_error(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"%PDF-1.4 not really a pdf")
    with pytest.raises(ParseError):
        parse(p)


# ---------- DOCX ----------
@pytest.mark.parametrize("wps", [False, True], ids=["Heading样式", "WPS标题样式+大纲级别"])
def test_docx_heading_path_and_ordinals(tmp_path, wps):
    p = make_docx(tmp_path / "plan.docx", ZH_PLAN, wps_style_names=wps)
    blocks = parse(p)
    first = blocks[0]
    assert first.text.startswith("本项目分两期")
    assert first.location == Location(heading_path="第三章 预算", para_start=2, para_end=2)
    budget = next(b for b in blocks if "一百二十万" in b.text)
    assert budget.location.heading_path == "第三章 预算 > 3.2 二期"
    assert budget.location.para_start == 4
    assert budget.location.page is None  # docx 不承诺页码


def test_docx_table_rows_become_blocks(tmp_path):
    p = make_docx(tmp_path / "t.docx", [("h1", "表"), ("table", "项目|金额;硬件|四十万")])
    rows = [b.text for b in parse(p)]
    assert rows == ["项目 | 金额", "硬件 | 四十万"]


def test_docx_heading_resets_deeper_levels(tmp_path):
    p = make_docx(tmp_path / "h.docx", [
        ("h1", "A"), ("h2", "A1"), ("p", "x"), ("h1", "B"), ("p", "y"),
    ])
    locs = {b.text: b.location.heading_path for b in parse(p)}
    assert locs == {"x": "A > A1", "y": "B"}


def test_docx_ole_container_is_treated_as_encrypted(tmp_path):
    p = tmp_path / "enc.docx"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
    with pytest.raises(EncryptedFileError):
        parse(p)


def test_docx_corrupt_zip_raises_parse_error(tmp_path):
    p = tmp_path / "bad.docx"
    p.write_bytes(b"PK\x03\x04garbage")
    with pytest.raises(ParseError):
        parse(p)


# ---------- Markdown ----------
def test_markdown_line_numbers_and_headings():
    blocks = list(parse_markdown_text(EN_POLICY_MD))
    by_text = {b.text.split("\n")[0][:20]: b.location for b in blocks}
    loc = by_text["Application logs are"]
    assert loc.heading_path == "Security Policy > Log retention"
    assert (loc.line_start, loc.line_end) == (8, 8)
    # 代码块中的 '#' 不是标题，代码块整体作为一个 Block
    code = next(b for b in blocks if b.text.startswith("```"))
    assert "# this is a code comment" in code.text
    assert code.location.heading_path == "Security Policy > Passwords"
    assert (code.location.line_start, code.location.line_end) == (14, 17)


def test_markdown_front_matter_skipped():
    blocks = list(parse_markdown_text(EN_POLICY_MD))
    assert not any("title: Security policy" in b.text for b in blocks)


def test_markdown_multiline_paragraph_range():
    text = "# T\n\nline one\nline two\n\n## Sub\ntext under sub"
    blocks = list(parse_markdown_text(text))
    assert blocks[0].text == "line one\nline two"
    assert (blocks[0].location.line_start, blocks[0].location.line_end) == (3, 4)
    assert blocks[1].location.heading_path == "T > Sub"
    assert blocks[1].location.line_start == 7


def test_markdown_without_headings():
    blocks = list(parse_markdown_text("just text\n\nmore"))
    assert [b.location.heading_path for b in blocks] == [None, None]
    assert blocks[1].location.line_start == 3


def test_markdown_gbk_encoding(tmp_path):
    p = tmp_path / "gbk.md"
    p.write_bytes("# 标题\n\n中文内容".encode("gb18030"))
    assert parse(p)[0].text == "中文内容"


def test_markdown_bom(tmp_path):
    p = tmp_path / "bom.md"
    p.write_bytes("﻿# H\n\nbody".encode("utf-8"))
    b = parse(p)[0]
    assert b.text == "body" and b.location.heading_path == "H"


def test_unexpected_exception_wrapped_as_parse_error(tmp_path):
    class Boom:
        extensions = (".x",)
        file_type = "x"

        def parse(self, path):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover

    with pytest.raises(ParseError, match="RuntimeError: kaboom"):
        parse_file(Boom(), tmp_path / "f.x")
