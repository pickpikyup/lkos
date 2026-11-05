"""PPTX、旧版 .doc、旧版 .ppt。"""

import shutil
import subprocess
import sys

import pytest

from lkos.app import App
from lkos.errors import EncryptedFileError, ParseError, UnsupportedFileError
from lkos.gateway.fake import FakeProvider
from lkos.parsers import parse_file, parser_for
from lkos.types import Location
from tests.conftest import TEST_DIM, fake_overrides
from tests.samples import make_docx, make_pptx

SLIDES = [
    {"title": "二期项目汇报", "body": ["预算上限为一百二十万元", "硬件采购不超过四十万元"],
     "notes": "讲到这里强调预算已经过评审"},
    {"title": "Roadmap", "body": ["Phase two starts in March"], "table": [["阶段", "时间"], ["二期", "三月"]]},
]


def test_pptx_slides_titles_tables_notes(tmp_path):
    p = make_pptx(tmp_path / "汇报.pptx", SLIDES)
    parser = parser_for(p)
    assert parser.file_type == "pptx"
    blocks = parse_file(parser, p)
    texts = [b.text for b in blocks]
    assert texts[:3] == ["二期项目汇报", "预算上限为一百二十万元", "硬件采购不超过四十万元"]
    assert "阶段 | 时间" in texts and "二期 | 三月" in texts
    note = next(b for b in blocks if "评审" in b.text)
    assert note.location.page == 1 and note.location.heading_path == "二期项目汇报 > 备注"
    table_row = next(b for b in blocks if b.text == "二期 | 三月")
    assert table_row.location.page == 2 and table_row.location.heading_path == "Roadmap"
    assert Location(page=2, heading_path="Roadmap").describe("pptx") == "幻灯片 2 · Roadmap"


def test_pptx_encrypted_or_renamed_ppt(tmp_path):
    p = tmp_path / "x.pptx"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64)
    with pytest.raises(EncryptedFileError):
        parse_file(parser_for(p), p)


def test_doc_converted_with_textutil_on_macos(tmp_path, monkeypatch):
    src = make_docx(tmp_path / "src.docx", [("h1", "第一章"), ("p", "二期预算上限为一百二十万元。")])
    doc = tmp_path / "旧文件.doc"
    doc.write_bytes(b"fake old doc")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        shutil.copy(src, cmd[cmd.index("-output") + 1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", fake_run)
    parser = parser_for(doc)
    blocks = parse_file(parser, doc)
    assert calls[0][:3] == ["textutil", "-convert", "docx"] and calls[0][-1] == str(doc)
    assert blocks[0].text == "二期预算上限为一百二十万元。" and blocks[0].location.heading_path == "第一章"
    assert blocks[0].location.describe("doc").startswith("第一章 · 段落")


def test_doc_conversion_failure_and_other_systems(tmp_path, monkeypatch):
    doc = tmp_path / "坏.doc"
    doc.write_bytes(b"x")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "error"))
    with pytest.raises(ParseError):
        parse_file(parser_for(doc), doc)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(UnsupportedFileError):
        parse_file(parser_for(doc), doc)


def test_index_pptx_and_skip_ppt_with_hint(tmp_path):
    docs = tmp_path / "d"
    make_pptx(docs / "汇报.pptx", SLIDES)
    (docs / "旧汇报.ppt").write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 32)
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        rep = a.indexer.index_folder(docs)
        assert rep.files_indexed == 1 and rep.files_skipped == 1 and rep.files_failed == 0
        hits = a.retriever.search("二期预算上限", k=3)
        assert hits[0].file_type == "pptx" and hits[0].describe().startswith("汇报.pptx · 幻灯片 1")
        bad = a.repo.failed_files()
        assert bad[0].status == "skipped" and bad[0].error.startswith("unsupported: 旧版 .ppt")
    finally:
        a.close()

    from lkos.ui.bridge import Bridge

    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    try:
        problems = b.library()["problems"]
        assert problems[0]["file"] == "旧汇报.ppt" and "另存为 .pptx" in problems[0]["hint"]
    finally:
        b._close()
