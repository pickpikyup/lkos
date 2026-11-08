"""每个文件夹的设置：更新周期、排除规则、文字识别开关。"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lkos.app import App
from lkos.gateway.fake import FakeProvider
from lkos.indexing.indexer import is_excluded
from lkos.indexing.scheduler import due_sources
from lkos.ui.bridge import Bridge
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
        assert r["reindex"] is True and r["exclude"]["patterns"] == ["归档"] and r["changes"] == "排除规则"
        time.sleep(0.5)
        files = b._call(lambda: [f.path for f in b._app.repo.files_of_source(sid)])
        assert any(p.endswith("旧报告.pdf") for p in files)                 # 不自动重建：先问用户
        r = b.set_source_settings(sid, {"ocr": True})
        assert r["changes"] == "排除规则、打开文字识别" and r["pdfs"] >= 1
        assert b.library()["sources"][0]["pending"] == "排除规则、打开文字识别"
        b.update_source(b.library()["sources"][0]["path"])                  # 用户点“现在更新”
        end = time.time() + 10
        while time.time() < end:
            files = b._call(lambda: [f.path for f in b._app.repo.files_of_source(sid)])
            if not any(p.endswith("旧报告.pdf") for p in files) and b._indexer.state["state"] == "idle":
                break
            time.sleep(0.1)
        assert not any(p.endswith("旧报告.pdf") for p in files)
        end = time.time() + 5
        while time.time() < end and b.library()["sources"][0]["pending"]:
            time.sleep(0.1)
        lib = b.library()
        assert lib["sources"][0]["exclude"]["patterns"] == ["归档"] and "ocr_available" in lib
        assert lib["sources"][0]["pending"] == ""                            # 更新完：提示消失
    finally:
        b._close()


def test_file_type_groups_follow_parsers(tmp_path):
    """界面上的文件类型来自解析器注册表：每种能识别的扩展名都要出现，新增格式时自动同步。"""
    from lkos.config import DEFAULT_EXTENSIONS
    from lkos.parsers.base import _registry, file_type_groups

    groups = file_type_groups(list(DEFAULT_EXTENSIONS))
    exts = {e for g in groups for e in g["exts"]}
    assert exts == {e.lstrip(".") for e in DEFAULT_EXTENSIONS}
    types = {t for g in groups for t in g["types"]}
    assert types == {p.file_type for p in _registry() if set(p.extensions) & set(DEFAULT_EXTENSIONS)}
    assert all(getattr(p, "label", "") for p in _registry())                 # 新解析器要写 label
    ppt = next(g for g in groups if g["label"] == "PowerPoint")
    assert ppt["indexable"] and ppt["skipped_exts"] == ["ppt"]
    assert {g["label"] for g in file_type_groups()} >= {"PDF", "Word", "PowerPoint", "Markdown"}

    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    try:
        assert b.boot()["file_types"] == groups
    finally:
        b._close()


def test_file_vanishes_during_indexing(tmp_path, docs):
    """扫描后、处理前文件被移走：跳过，不中断整个文件夹（实际遇到过：一边整理文件夹一边索引）。"""
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        real = list(a.indexer.walk(docs))
        a.indexer.walk = lambda folder: iter([docs / "已经移走.pdf", *real])
        rep = a.indexer.index_folder(docs)
        assert rep.files_indexed == len(real) and rep.files_failed == 0
    finally:
        a.close()


def test_index_worker_survives_unexpected_error(tmp_path, docs):
    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    try:
        calls = []
        import lkos.ui.bridge as br

        real_index = br.IndexWorker._index

        def boom(self, app, folder, background):
            calls.append(folder)
            if len(calls) == 1:
                raise RuntimeError("意外错误")
            return real_index(self, app, folder, background)

        br.IndexWorker._index = boom
        try:
            b.add_folder(str(docs))
            end = time.time() + 5
            while time.time() < end and not (b._indexer.state.get("last") or {}).get("error"):
                time.sleep(0.05)
            assert "意外错误" in b._indexer.state["last"]["error"]
            b.update_source(str(docs))                       # 线程还活着：第二次正常完成
            end = time.time() + 10
            while time.time() < end and "indexed" not in (b._indexer.state.get("last") or {}):
                time.sleep(0.05)
            assert b._indexer.state["last"]["indexed"] == 4 and b._indexer._thread.is_alive()
        finally:
            br.IndexWorker._index = real_index
    finally:
        b._close()


def test_background_refresh_waits_for_idle_until_run_now(tmp_path, docs, monkeypatch):
    """后台定期更新等电脑空闲；点“现在就更新”后不再等。从没索引完的文件夹打开应用时直接接着做。"""
    from lkos.indexing.scheduler import IndexGate

    monkeypatch.setattr(IndexGate, "blocker", lambda self: "busy")       # 一直在用电脑
    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    try:
        def wait(cond, t=10):
            end = time.time() + t
            while time.time() < end and not cond():
                time.sleep(0.05)
            return cond()

        b._call(lambda: b._app.repo.upsert_source(str(docs)))          # 添加了但从没索引完
        b._call(lambda: b._indexer._enqueue_stale(b._app))
        assert wait(lambda: "indexed" in (b._indexer.state.get("last") or {}))   # 不等空闲，直接做完
        b._indexer.enqueue(str(docs), background=True)                  # 定期更新：等空闲
        assert wait(lambda: b._indexer.state["state"] == "paused" and b._indexer.state["reason"] == "busy")
        b._indexer.state["last"] = None
        assert b.run_now_index()["ok"]
        assert wait(lambda: "indexed" in (b._indexer.state.get("last") or {}) and b._indexer.state["state"] == "idle"
                    and b._indexer._jobs.empty()), b._indexer.state      # 排队的（含生成标签）都做完
        assert wait(lambda: not b._indexer._run_now.is_set())            # 之后恢复为等空闲
        b._indexer.enqueue(str(docs), background=True)
        assert wait(lambda: b._indexer.state["state"] == "paused")
    finally:
        b._close()
