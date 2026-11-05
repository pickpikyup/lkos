"""每个文件夹的设置：更新周期、排除规则、文字识别开关。"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lkos.app import App
from lkos.gateway.fake import FakeProvider
from lkos.indexing.indexer import is_excluded
from lkos.ui.bridge import Bridge, due_sources
from tests.conftest import TEST_DIM, fake_overrides
from tests.samples import make_docx, make_pdf


def test_is_excluded_rules():
    assert is_excluded(Path("归档/a.pdf"), ["归档"])
    assert is_excluded(Path("x/2023 草稿 v2.docx"), ["*草稿*"])
    assert is_excluded(Path("2019/会议/a.pdf"), ["2019/*"])
    assert is_excluded(Path("项目/旧版/a.pdf"), ["项目/旧版"])
    assert is_excluded(Path("Archive/a.pdf"), ["archive"])                 # 不区分大小写
    assert not is_excluded(Path("项目/新版/a.pdf"), ["项目/旧版", "归档", ""])
    assert not is_excluded(Path("a/2019/b.pdf"), ["2019/*"])                # 含“/”的规则从根开始


def test_due_sources_by_refresh(tmp_path):
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    ago = lambda d: (now - timedelta(days=d)).isoformat(timespec="seconds")  # noqa: E731
    p = str(tmp_path)
    src = [{"id": 1, "path": p, "refresh": "daily", "last_indexed": ago(2)},
           {"id": 2, "path": p, "refresh": "weekly", "last_indexed": ago(3)},
           {"id": 3, "path": p, "refresh": "monthly", "last_indexed": ago(31)},
           {"id": 4, "path": p, "refresh": "manual", "last_indexed": ago(400)},
           {"id": 5, "path": p, "refresh": "manual", "last_indexed": None},          # 没索引完：继续
           {"id": 6, "path": str(tmp_path / "没有"), "refresh": "daily", "last_indexed": None}]
    assert [s["id"] for s in due_sources(src, now)] == [1, 3, 5]


@pytest.fixture
def docs(tmp_path):
    d = tmp_path / "docs"
    make_pdf(d / "报告.pdf", ["二期预算上限一百二十万元。"])
    make_pdf(d / "归档" / "旧报告.pdf", ["一期预算八十万元。"])
    make_docx(d / "草稿" / "计划.docx", [("p", "计划草稿：预算待定。")])
    (d / "说明.md").write_text("# 说明\n\n资料说明文档，介绍目录结构。\n", encoding="utf-8")
    return d


def test_excludes_apply_and_remove_indexed(tmp_path, docs):
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        a.indexer.index_folder(docs)
        assert len(list(a.indexer.walk(docs))) == 4
        src = a.repo.list_sources()[0]
        assert src["refresh"] == "weekly" and src["exclude"] == {"patterns": [], "types": []} and src["ocr"] is False
        a.repo.update_source_settings(src["id"], refresh="monthly", exclude={"patterns": ["归档", " ", "草稿/*"], "types": [".MD"]},
                                      ocr=True)
        s = a.repo.source_settings(src["path"])
        assert s["refresh"] == "monthly" and s["ocr"] is True
        assert s["exclude"] == {"patterns": ["归档", "草稿/*"], "types": ["md"]}
        assert [p.name for p in a.indexer.walk(docs)] == ["报告.pdf"]
        rep = a.indexer.index_folder(docs)
        assert rep.files_removed == 3 and {f.path.rsplit("/", 1)[-1] for f in a.repo.files_of_source(src["id"])} == {"报告.pdf"}
        with pytest.raises(ValueError):
            a.repo.update_source_settings(src["id"], refresh="hourly")
    finally:
        a.close()


def test_bridge_folder_settings(tmp_path, docs):
    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    try:
        b._call(lambda: b._app.indexer.index_folder(docs))
        sid = b.library()["sources"][0]["id"]
        cur = b.source_settings(sid)
        assert cur["refresh"] == "weekly" and "ocr_available" in cur
        assert b.source_settings(999)["error"]
        r = b.set_source_settings(sid, {"refresh": "daily"})
        assert r["refresh"] == "daily" and r["reindex"] is False
        assert b.set_source_settings(sid, {"refresh": "yearly"})["error"]
        r = b.set_source_settings(sid, {"exclude": {"patterns": ["归档"], "types": []}})
        assert r["reindex"] is True and r["exclude"]["patterns"] == ["归档"]
        end = time.time() + 10
        while time.time() < end:
            files = b._call(lambda: [f.path for f in b._app.repo.files_of_source(sid)])
            if not any(p.endswith("旧报告.pdf") for p in files) and b._indexer.state["state"] == "idle":
                break
            time.sleep(0.1)
        assert not any(p.endswith("旧报告.pdf") for p in files)
        lib = b.library()
        assert lib["sources"][0]["exclude"]["patterns"] == ["归档"] and "ocr_available" in lib
    finally:
        b._close()
