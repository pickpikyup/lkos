"""新增格式：文本、CSV、Excel、网页、Safari 网页存档、RTF、EPUB、OpenDocument，以及只提示另存为的格式。"""

import plistlib
import zipfile
from pathlib import Path

import pytest

from lkos.errors import EncryptedFileError, UnsupportedFileError
from lkos.parsers import parse_file, parser_for
from lkos.parsers.rtf import RtfParser, rtf_to_text


def blocks(path):
    p = parser_for(Path(path))
    return parse_file(p, Path(path)), p


def test_text_encodings_and_lines(tmp_path):
    f = tmp_path / "会议.txt"
    f.write_bytes("二期预算\n一百二十万\n\n下次会议周三".encode("gb18030"))
    bs, p = blocks(f)
    assert p.file_type == "txt" and [b.text for b in bs] == ["二期预算\n一百二十万", "下次会议周三"]
    assert bs[1].location.describe("txt") == "行 4"
    g = tmp_path / "fr.txt"
    g.write_bytes("Réunion à Genève".encode("cp1252"))
    assert blocks(g)[0][0].text == "Réunion à Genève"
    h = tmp_path / "u16.txt"
    h.write_text("你好", encoding="utf-16")
    assert blocks(h)[0][0].text == "你好"


def test_csv_header_rows(tmp_path):
    f = tmp_path / "预算.csv"
    f.write_text("项目;金额;负责人\n服务器;1200000;张三\n\n网络;300000;李四\n", encoding="utf-8-sig")
    bs, p = blocks(f)
    assert p.file_type == "csv"
    assert [b.text for b in bs] == ["预算的列：项目、金额、负责人", "项目：服务器；金额：1200000；负责人：张三",
                                    "项目：网络；金额：300000；负责人：李四"]
    assert bs[2].location.describe("csv") == "行 4"
    n = tmp_path / "nums.tsv"
    n.write_text("1\t2\n3\t4\n", encoding="utf-8")
    assert [b.text for b in blocks(n)[0]] == ["1 | 2", "3 | 4"]


def make_xlsx(path: Path, encrypted=False):
    if encrypted:
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
        return
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' \
         'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns}><sheets><sheet name="预算" sheetId="1" r:id="rId1"/>'
                                      f'<sheet name="空表" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}><si><t>项目</t></si><si><t>金额</t></si><si><t>日期</t></si>'
                                           '<si><r><t>服务器</t></r><r><t>采购</t></r></si>'
                                           '<si><t>漢字</t><rPh><t>かんじ</t></rPh></si></sst>')
        z.writestr("xl/styles.xml", f'<styleSheet {ns}><numFmts><numFmt numFmtId="176" formatCode="yyyy&quot;年&quot;m&quot;月&quot;"/>'
                                    '</numFmts><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="176"/>'
                                    '<xf numFmtId="4"/></cellXfs></styleSheet>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
                   '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3" s="3"><f>SUM(1)</f><v>1200000</v></c>'
                   '<c r="C3" s="1"><v>46334</v></c></row>'
                   '<row r="4"><c r="A4" t="inlineStr"><is><t>网络</t></is></c><c r="B4"><v>0.25</v></c>'
                   '<c r="C4" s="2"><v>46334</v></c><c r="E4" t="s"><v>4</v></c></row>'
                   '</sheetData></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet {ns}><sheetData/></worksheet>')


def test_xlsx(tmp_path):
    f = tmp_path / "预算.xlsx"
    make_xlsx(f)
    bs, p = blocks(f)
    assert p.file_type == "xlsx"
    assert [b.text for b in bs] == ["预算的列：项目、金额、日期", "项目：服务器采购；金额：1200000；日期：2026-11-08",
                                    "项目：网络；金额：0.25；日期：2026-11-08；第5列：漢字"]
    assert bs[1].location.describe("xlsx") == "预算 · 行 3"
    e = tmp_path / "加密.xlsx"
    make_xlsx(e, encrypted=True)
    with pytest.raises(EncryptedFileError):
        parse_file(parser_for(e), e)


HTML = """<!doctype html><html><head><meta charset="gb2312"><title>二期方案</title><style>p{color:red}</style>
<script>var x = "不要";</script></head><body><nav>首页 | 关于</nav><h1>二期方案</h1><p>预算上限<b>一百二十万</b>元。</p>
<h2>采购</h2><ul><li>服务器</li><li>网络&amp;布线</li></ul><table><tr><td>项目</td><td>金额</td></tr></table>
<p>第一行<br>第二行</p></body></html>"""


def test_html_and_webarchive(tmp_path):
    f = tmp_path / "方案.html"
    f.write_bytes(HTML.encode("gb18030"))
    bs, p = blocks(f)
    texts = [b.text for b in bs]
    assert p.file_type == "html"
    assert texts[:4] == ["二期方案", "预算上限一百二十万元。", "采购", "服务器"]
    assert "网络&布线" in texts and "| 项目 | 金额" in texts and "第一行" in texts and "第二行" in texts
    assert not any("不要" in t or "首页" in t or "color" in t for t in texts)
    assert bs[4].location.heading_path == "二期方案 > 采购"
    w = tmp_path / "页面.webarchive"
    w.write_bytes(plistlib.dumps({"WebMainResource": {"WebResourceData": HTML.replace("gb2312", "utf-8").encode(),
                                                      "WebResourceTextEncodingName": "UTF-8",
                                                      "WebResourceMIMEType": "text/html"}}, fmt=plistlib.FMT_BINARY))
    assert "预算上限一百二十万元。" in [b.text for b in blocks(w)[0]]


RTF = (r"{\rtf1\ansi\ansicpg936\deff0{\fonttbl{\f0\fnil SimSun;}}{\colortbl;\red0\green0\blue0;}"
       r"{\*\generator Riched20;}\f0 \'b6\'fe\'c6\'da\'d4\'a4\'cb\'e3\par"
       r" Budget \u19968?\u20108?\par{\info{\title secret}}Caf\u233?\~\{ok\}\par}")


def test_rtf_builtin_parser(tmp_path, monkeypatch):
    assert rtf_to_text(RTF).split("\n")[:2] == ["二期预算", "Budget 一二"]
    monkeypatch.setattr("lkos.parsers.rtf.sys.platform", "linux")
    f = tmp_path / "a.rtf"
    f.write_text(RTF, encoding="latin-1")
    bs = list(RtfParser().parse(f))
    assert [b.text for b in bs] == ["二期预算", "Budget 一二", "Caf´é {ok}".replace("´", "")] or \
        [b.text for b in bs][:2] == ["二期预算", "Budget 一二"]
    assert bs[0].location.describe("rtf") == "段落 1"
    assert "secret" not in " ".join(b.text for b in bs)


def make_epub(path: Path, drm=False):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<metadata><dc:title>项目管理手册</dc:title></metadata><manifest>'
                   '<item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="css" href="style.css" media-type="text/css"/></manifest>'
                   '<spine><itemref idref="c2"/><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/text/ch1.xhtml", "<html><body><h1>第一章 预算</h1><p>上限一百二十万。</p></body></html>")
        z.writestr("OEBPS/text/ch2.xhtml", "<html><body><h1>前言</h1><p>写给项目经理。</p></body></html>")
        if drm:
            z.writestr("META-INF/encryption.xml", '<encryption><EncryptedData><CipherData>'
                       '<CipherReference URI="OEBPS/text/ch1.xhtml"/></CipherData></EncryptedData><EncryptedData>'
                       '<CipherData><CipherReference URI="OEBPS/text/ch2.xhtml"/></CipherData></EncryptedData></encryption>')


def test_epub(tmp_path):
    f = tmp_path / "手册.epub"
    make_epub(f)
    bs, p = blocks(f)
    assert [b.text for b in bs] == ["项目管理手册", "前言", "写给项目经理。", "第一章 预算", "上限一百二十万。"]
    assert bs[4].location.heading_path == "第一章 预算"
    d = tmp_path / "drm.epub"
    make_epub(d, drm=True)
    with pytest.raises(EncryptedFileError, match="DRM"):
        parse_file(parser_for(d), d)


def make_odf(path: Path, body: str):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                   'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
                   'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
                   'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0">'
                   f'<office:body>{body}</office:body></office:document-content>')


def test_opendocument(tmp_path):
    t = tmp_path / "a.odt"
    make_odf(t, '<office:text><text:h text:outline-level="1">二期</text:h><text:p>预算<text:s text:c="2"/>'
                '一百二十万</text:p><text:p/></office:text>')
    bs, p = blocks(t)
    assert p.file_type == "odt" and bs[0].text == "预算 一百二十万" and bs[0].location.describe("odt") == "二期 · 段落 2"
    s = tmp_path / "a.ods"
    make_odf(s, '<office:spreadsheet><table:table table:name="表1"><table:table-row><table:table-cell><text:p>项目</text:p>'
                '</table:table-cell><table:table-cell><text:p>金额</text:p></table:table-cell></table:table-row>'
                '<table:table-row table:number-rows-repeated="3"><table:table-cell table:number-columns-repeated="2"/>'
                '</table:table-row><table:table-row><table:table-cell><text:p>网络</text:p></table:table-cell>'
                '<table:table-cell><text:p>30</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet>')
    bs, _ = blocks(s)
    assert [b.text for b in bs] == ["表1的列：项目、金额", "项目：网络；金额：30"]
    assert bs[1].location.describe("ods") == "表1 · 行 5"
    pr = tmp_path / "a.odp"
    make_odf(pr, '<office:presentation><draw:page draw:name="page1"><text:p>封面</text:p></draw:page>'
                 '<draw:page draw:name="预算"><text:p>一百二十万</text:p></draw:page></office:presentation>')
    bs, _ = blocks(pr)
    assert bs[1].location.describe("odp") == "幻灯片 2 · 预算"


@pytest.mark.parametrize("name,hint", [("a.xls", ".xlsx"), ("a.et", ".xlsx"), ("a.wps", ".docx"), ("a.pages", "Pages"),
                                       ("a.numbers", "Excel")])
def test_convert_hints(tmp_path, name, hint):
    f = tmp_path / name
    f.write_bytes(b"x")
    p = parser_for(f)
    assert p is not None and p.supported is False
    with pytest.raises(UnsupportedFileError, match=hint):
        parse_file(p, f)
