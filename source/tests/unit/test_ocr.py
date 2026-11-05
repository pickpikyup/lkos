"""文字识别：扫描页识别、只识别没有文字层的页面、打开开关后重新处理、闸门可中断。"""

from pathlib import Path

import pymupdf as fitz
import pytest

from lkos.app import App
from lkos.errors import Cancelled
from lkos.gateway.fake import FakeProvider
from lkos.parsers import ocr
from lkos.parsers.base import parse_file
from lkos.parsers.pdf import PdfParser
from tests.conftest import TEST_DIM, fake_overrides


def make_scanned_pdf(path: Path, text_pages: dict[int, str], pages: int) -> Path:
    """pages 页的 PDF：text_pages 里的页有文字层，其余页只有一张图片（模拟扫描页）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40), False)
    pix.clear_with(200)
    png = pix.tobytes("png")
    for i in range(1, pages + 1):
        page = doc.new_page()
        if i in text_pages:
            page.insert_text((72, 72), text_pages[i], fontname="china-s")
        else:
            page.insert_image(fitz.Rect(72, 72, 400, 300), stream=png)
    doc.save(str(path))
    doc.close()
    return path


class FakeOcr:
    def __init__(self, text="扫描件识别出的文字：三期预算上限二百万元，由信息中心负责实施。"):
        self.text = text
        self.calls = 0

    def __call__(self, png: bytes) -> str:
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        self.calls += 1
        return self.text


def test_join_lines():
    assert ocr.join_lines(["项目二期的建设", "内容包括服务器采购。", "第二段开始", "", "English line", "next line"]) == \
        "项目二期的建设内容包括服务器采购。\n第二段开始\nEnglish line\nnext line"
    assert ocr.available() is False or ocr.engine() is not None


def test_pdf_parser_ocr_only_scanned_pages(tmp_path):
    p = make_scanned_pdf(tmp_path / "混合.pdf", {1: "Cover page with enough typed text here."}, pages=3)
    assert [b.location.page for b in PdfParser().parse(p)] == [1]          # 不识别：扫描页没有内容
    eng = FakeOcr()
    blocks = list(PdfParser().parse(p, ocr=eng))
    assert eng.calls == 2 and [b.location.page for b in blocks] == [1, 2, 3]
    assert "三期预算" in blocks[1].text


def test_cancel_from_gate_is_not_wrapped(tmp_path):
    p = make_scanned_pdf(tmp_path / "扫描.pdf", {}, pages=2)

    def stop():
        raise Cancelled("已停止索引")

    with pytest.raises(Cancelled):
        parse_file(PdfParser(), p, ocr=FakeOcr(), before_ocr=stop)


def test_index_with_ocr_toggle(tmp_path):
    docs = tmp_path / "docs"
    make_scanned_pdf(docs / "扫描合同.pdf", {}, pages=2)
    make_scanned_pdf(docs / "混合报告.pdf", {1: "Annual report cover page with typed text."}, pages=2)
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        eng = FakeOcr()
        a.indexer.ocr_engine = eng
        rep = a.indexer.index_folder(docs)
        assert eng.calls == 0 and rep.files_skipped == 1 and rep.files_indexed == 1   # 没打开：扫描合同跳过
        src = a.repo.list_sources()[0]
        a.repo.update_source_settings(src["id"], ocr=True)
        assert a.repo.get_meta(f"ocr:{src['id']}")
        rep = a.indexer.index_folder(docs)
        assert rep.files_indexed == 2 and eng.calls == 3                 # 扫描合同 2 页 + 混合报告第 2 页
        hits = a.retriever.search("三期预算上限", k=5)
        assert hits and {h.file_name for h in hits} >= {"扫描合同.pdf"}
        assert any(h.location.page == 2 for h in hits)
        rep = a.indexer.index_folder(docs)                                # 再次更新：不重复识别
        assert eng.calls == 3 and rep.files_indexed == 0
        a.repo.update_source_settings(src["id"], ocr=True)               # 已经是开：不重置
        a.indexer.index_folder(docs)
        assert eng.calls == 3
    finally:
        a.close()


def test_ocr_unavailable_keeps_skipping(tmp_path):
    docs = tmp_path / "docs"
    make_scanned_pdf(docs / "扫描.pdf", {}, pages=1)
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        a.indexer.ocr_engine = None
        a.indexer.index_folder(docs)
        a.repo.update_source_settings(a.repo.list_sources()[0]["id"], ocr=True)
        events = []
        a.indexer.index_folder(docs, on_progress=events.append)
        assert a.repo.failed_files()[0].error == "no extractable text"
    finally:
        a.close()
