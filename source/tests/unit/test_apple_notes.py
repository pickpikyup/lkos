"""Mac 备忘录：HTML 转 Markdown、导出（增量、改名、删除、跳过加锁与最近删除）、索引与检索、打开。"""

import json
from pathlib import Path
import os
import time

import pytest

from lkos.app import App
from lkos.gateway.fake import FakeProvider
from lkos.retrieval.keyword import keyword_search
from lkos.sources import apple_notes
from lkos.sources.apple_notes import html_to_markdown, sync
from tests.conftest import TEST_DIM, fake_overrides


def note(nid, name, body, folder=("iCloud", "工作"), modified="2026-11-01T08:00:00.000Z", locked=False):
    return {"id": nid, "name": name, "path": list(folder), "modified": modified, "created": "2026-10-01T08:00:00.000Z",
            "locked": locked, "body": body}


NOTES = [
    note("x-coredata://1", "二期项目会议", "<div><h1>二期项目会议</h1></div><div>预算上限<b>一百二十万</b>元</div>"
         "<ul><li>服务器采购</li><li>网络改造</li></ul><div><br></div><div>下次会议&nbsp;周三</div>"),
    note("x-coredata://2", "购物清单", "<div>牛奶</div><div>鸡蛋</div>", folder=("iCloud", "生活")),
    note("x-coredata://3", "银行密码", "", locked=True),
    note("x-coredata://4", "旧笔记", "<div>已经删除的内容</div>", folder=("iCloud", "最近删除")),
    note("x-coredata://5", "二期项目会议", "<div>同名的另一条备忘录：进度汇报</div>"),
    dict(note("x-coredata://6", "家庭共享清单", "<div>共享内容</div>", folder=("iCloud", "家庭")), shared=True),
]


def test_html_to_markdown():
    md = html_to_markdown("<div><h1>标题</h1></div><div>第一行<br>第二行</div><ol><li>一</li><li>二</li></ol>"
                          "<div><b>粗体</b>&amp;符号&nbsp;空格</div><ul><li>甲<ul><li>乙</li></ul></li></ul>")
    assert md.splitlines() == ["## 标题", "第一行", "第二行", "1. 一", "2. 二", "**粗体**&符号 空格", "- 甲", "  - 乙"]
    assert html_to_markdown("") == ""


def test_sync_incremental(tmp_path):
    home = tmp_path / "h"
    r = sync(home, lambda: NOTES)
    assert r == {"written": 3, "removed": 0, "skipped": 2, "shared": 1, "total": 3}
    root = apple_notes.export_dir(home)
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*.md"))
    assert files == ["iCloud/工作/二期项目会议 (2).md", "iCloud/工作/二期项目会议.md", "iCloud/生活/购物清单.md"]
    text = (root / "iCloud/工作/二期项目会议.md").read_text("utf-8")
    assert text.startswith("# 二期项目会议\n\n预算上限**一百二十万**元") and "- 服务器采购" in text and "## 二期" not in text
    assert "下次会议 周三" in text and "备忘录 · iCloud / 工作 · 2026-11-01" in text
    mtime = os.path.getmtime(root / "iCloud/工作/二期项目会议.md")
    assert abs(mtime - 1793520000) < 2                                       # 修改时间 = 备忘录的修改时间
    assert apple_notes.note_id_for(home, str(root / "iCloud/生活/购物清单.md")) == "x-coredata://2"
    # 不变：不重写
    assert sync(home, lambda: NOTES)["written"] == 0
    # 修改一条、删除一条、改名一条
    changed = [dict(NOTES[0], body="<div>预算改为一百五十万</div>", modified="2026-11-05T08:00:00.000Z"),
               dict(NOTES[1], name="周末购物"), NOTES[2], NOTES[3]]
    r = sync(home, lambda: changed)
    assert r["written"] == 2 and r["removed"] == 1 and r["total"] == 2          # 改名的那条在写入时换文件名
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*.md"))
    assert files == ["iCloud/工作/二期项目会议.md", "iCloud/生活/周末购物.md"]
    assert "一百五十万" in (root / "iCloud/工作/二期项目会议.md").read_text("utf-8")
    # 文件夹清空后删掉空文件夹
    sync(home, lambda: [NOTES[1]])
    assert not (root / "iCloud" / "工作").exists()


def test_index_and_search_notes(tmp_path, monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return NOTES

    monkeypatch.setattr(apple_notes, "fetch_notes", fake)
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        folder = apple_notes.export_dir(a.home)
        rep = a.indexer.index_folder(folder)                 # 索引备忘录文件夹时先同步
        assert calls and rep.files_indexed == 3
        hits = keyword_search(a.repo, "一百二十万")
        assert [f.name for f in hits.files] == ["二期项目会议.md"]
        assert all("银行" not in f.name and "旧笔记" not in f.name for f in keyword_search(a.repo, "笔记 内容").files)
        # 同步失败（没授权）：照旧索引上次导出的内容，不报错
        def denied():
            raise apple_notes.LkosError("没有权限读取备忘录")
        monkeypatch.setattr(apple_notes, "fetch_notes", denied)
        rep = a.indexer.index_folder(folder)
        assert rep.files_failed == 0 and len(a.repo.files_of_source(a.repo.list_sources()[0]["id"])) == 3
    finally:
        a.close()


def test_bridge_add_and_open_notes(tmp_path, monkeypatch):
    from lkos.ui.bridge import Bridge

    shown = []
    monkeypatch.setattr(apple_notes, "fetch_notes", lambda: NOTES)
    monkeypatch.setattr(apple_notes, "available", lambda: True)
    monkeypatch.setattr(apple_notes, "show_note", lambda nid: shown.append(nid))
    opened = []
    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False, opener=lambda p, reveal=False: opened.append(p))
    try:
        r = b.add_notes()
        assert r["ok"] and r["total"] == 3 and r["skipped"] == 2
        end = time.time() + 10
        while time.time() < end and not (b._indexer.state["state"] == "idle" and b._indexer.state.get("last")):
            time.sleep(0.05)
        lib = b.library()
        src = lib["sources"][0]
        assert src["kind"] == "apple_notes" and lib["notes_available"] and src["files"] == 3 and src["refresh"] == "daily"
        assert b.boot()["sources"][0]["kind"] == "apple_notes"
        path = str(apple_notes.export_dir(tmp_path / "h").resolve() / "iCloud/生活/购物清单.md")
        assert b.open_file(path)["ok"] and shown == ["x-coredata://2"] and not opened
        b.open_file(str(tmp_path / "别的文件.pdf"))
        assert opened                                        # 普通文件照常用系统程序打开
        monkeypatch.setattr(apple_notes, "available", lambda: False)
        assert b.add_notes()["error"]
    finally:
        b._close()


def test_fetch_reads_utf8_file_and_tolerates_bad_output(monkeypatch):
    """JXA 把结果以 UTF-8 写进文件（不经过 osascript 的标准输出）；控制字符、BOM 不影响解析。"""
    import subprocess

    data = [{"id": "x-coredata://9", "name": "表情 😀 与 Café", "path": ["iCloud", "备忘录"], "modified": "",
             "created": "", "locked": False, "body": "<div>第一行\u0001</div>"}]

    def fake_run(cmd, **kw):
        assert kw["env"]["LC_ALL"].endswith("UTF-8")
        Path(cmd[-1]).write_bytes(json.dumps(data, ensure_ascii=False).replace("\\u0001", "\u0001").encode("utf-8"))
        return subprocess.CompletedProcess(cmd, 0, stdout=b"ok:1", stderr=b"")

    monkeypatch.setattr(apple_notes, "available", lambda: True)
    monkeypatch.setattr(apple_notes.subprocess, "run", fake_run)
    notes = apple_notes.fetch_notes()
    assert notes[0]["name"] == "表情 😀 与 Café" and "第一行" in notes[0]["body"]
    assert apple_notes.parse_output("﻿[]") == [] and apple_notes.parse_output("ok:0") == []
    with pytest.raises(apple_notes.LkosError, match="无法解析"):
        apple_notes.parse_output('[{"id": "x", "name": 未加引号}]')


def test_add_notes_does_not_block_and_ignores_repeat_clicks(tmp_path, monkeypatch):
    """读取备忘录很慢时：界面其他操作照常（不占问答线程），重复点击直接返回“正在读取”，索引时不再马上重读。"""
    import threading

    from lkos.ui.bridge import Bridge

    started, release, calls = threading.Event(), threading.Event(), []

    def slow():
        calls.append(1)
        started.set()
        release.wait(10)
        return NOTES

    monkeypatch.setattr(apple_notes, "fetch_notes", slow)
    monkeypatch.setattr(apple_notes, "available", lambda: True)
    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    try:
        out = {}
        t = threading.Thread(target=lambda: out.update(b.add_notes()))
        t.start()
        assert started.wait(5)
        t0 = time.time()
        assert "sources" in b.library() and time.time() - t0 < 2          # 读取期间资料库照常打开
        assert b.add_notes() == {"busy": True}
        release.set()
        t.join(10)
        assert out["ok"] and out["total"] == 3
        end = time.time() + 10
        while time.time() < end and not (b._indexer.state["state"] == "idle" and b._indexer.state.get("last")):
            time.sleep(0.05)
        assert b._indexer.state["last"]["indexed"] == 3 and calls == [1]    # 刚读过：索引时没有再读一遍
    finally:
        b._close()
