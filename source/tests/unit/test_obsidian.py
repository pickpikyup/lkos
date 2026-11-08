"""Obsidian：语法清理、属性与标签、白板、仓库识别与发现、在 Obsidian 中打开；资料体检。"""

import json
import os
import time
from pathlib import Path

from lkos.app import App
from lkos.gateway.fake import FakeProvider
from lkos.parsers import parse_file, parser_for
from lkos.parsers.markdown import parse_markdown_text
from lkos.retrieval.keyword import keyword_search
from lkos.sources import obsidian
from tests.conftest import TEST_DIM, fake_overrides

NOTE = """---
title: 二期项目
aliases: [二期, Phase 2]
tags:
  - 项目/二期
  - 预算
status: 进行中
cssclasses: wide
---
# 二期项目会议

预算见 [[预算表|最终预算]]，负责人 [[张三]]，参考 [[规范#采购流程]]。
==上限一百二十万==，详见 [官网](https://example.com) ![截图](a.png) ![[图1.png]]
![[会议纪要]] ![[合同.pdf]]

> [!warning] 风险提示
> 服务器到货可能延期 ^risk1

%%这是私下的备注，不要索引%%
正文继续 #待办 #2026 `#不是标签`
%%
多行注释
也不要
%%
结尾

```dataview
TABLE file.name FROM #项目
```

```python
print("代码保留")
```
"""


def test_markdown_obsidian_cleanup():
    tags = []
    bs = list(parse_markdown_text(NOTE, obsidian=True, tags=tags))
    texts = [b.text for b in bs]
    assert texts[0] == "属性　title：二期项目；aliases：二期、Phase 2；tags：项目/二期、预算；status：进行中"
    assert list(parse_markdown_text("---\ntitle: 会议\n---\n# 会议\n正文"))[0].text == "正文"   # title 与标题重复时不写
    assert texts[1] == ("预算见 最终预算，负责人 张三，参考 规范 采购流程。\n上限一百二十万，详见 官网 截图 \n"
                        "（嵌入：会议纪要） （附件：合同.pdf）")
    assert "风险提示\n服务器到货可能延期" in texts
    assert all("私下" not in t and "多行注释" not in t and "TABLE" not in t for t in texts)
    assert "正文继续 #待办 #2026 `#不是标签`" in texts and "结尾" in texts and any("代码保留" in t for t in texts)
    assert tags == ["项目/二期", "预算", "待办"]
    # 行号对应原文件
    risk = next(b for b in bs if "风险" in b.text)
    assert NOTE.splitlines()[risk.location.line_start - 1].startswith("> [!warning]")
    assert bs[1].location.heading_path == "二期项目会议"
    # 不在仓库里：注释、查询代码块保留原样，不收集正文标签
    plain = list(parse_markdown_text(NOTE, obsidian=False, tags=(t2 := [])))
    assert any("私下" in b.text for b in plain) and any("TABLE" in b.text for b in plain) and t2 == ["项目/二期", "预算"]


def make_vault(root: Path) -> Path:
    v = root / "我的仓库"
    (v / ".obsidian").mkdir(parents=True)
    (v / ".obsidian" / "app.json").write_text("{}")
    (v / "项目").mkdir()
    (v / "项目" / "二期.md").write_text(NOTE, encoding="utf-8")
    (v / ".trash").mkdir()
    (v / ".trash" / "旧.md").write_text("已删除的笔记", encoding="utf-8")
    (v / "白板.canvas").write_text(json.dumps({
        "nodes": [
            {"id": "g", "type": "group", "label": "采购", "x": 0, "y": 0, "width": 1000, "height": 1000},
            {"id": "a", "type": "text", "text": "# 服务器\n预算八十万", "x": 10, "y": 10, "width": 200, "height": 100},
            {"id": "b", "type": "file", "file": "项目/二期.md", "x": 10, "y": 300, "width": 200, "height": 100},
            {"id": "c", "type": "link", "url": "https://example.com", "x": 2000, "y": 0, "width": 100, "height": 100},
        ],
        "edges": [{"id": "e", "fromNode": "a", "toNode": "b", "label": "依据"}]}, ensure_ascii=False), encoding="utf-8")
    return v


def test_canvas_parser(tmp_path):
    v = make_vault(tmp_path)
    f = v / "白板.canvas"
    p = parser_for(f)
    bs = parse_file(p, f)
    assert p.file_type == "canvas"
    assert [(b.text, b.location.heading_path) for b in bs] == [
        ("采购", None), ("（网页：https://example.com）", None), ("预算八十万", "采购 > 服务器"), ("（卡片引用：二期）", "采购"),
        ("服务器 → 二期：依据", None)]


def test_vault_detection_and_discovery(tmp_path):
    v = make_vault(tmp_path)
    assert obsidian.vault_of(v / "项目" / "二期.md") == str(v) and obsidian.vault_of(tmp_path / "x.md") is None
    assert obsidian.is_vault(v) and not obsidian.is_vault(tmp_path)
    cfg = tmp_path / "obsidian.json"
    cfg.write_text(json.dumps({"vaults": {"1": {"path": str(v), "ts": 2, "open": True},
                                          "2": {"path": str(tmp_path / "不存在"), "ts": 1}}}))
    found = obsidian.discover(cfg)
    assert [(x["name"], x["exists"], x["open"]) for x in found] == [("我的仓库", True, True), ("不存在", False, False)]
    assert obsidian.discover(tmp_path / "没有.json") == []
    assert obsidian.open_url("/Users/x/仓库/a b.md") == "obsidian://open?path=%2FUsers%2Fx%2F%E4%BB%93%E5%BA%93%2Fa%20b.md"


def test_index_vault_tags_and_search(tmp_path):
    v = make_vault(tmp_path)
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        rep = a.indexer.index_folder(v)
        assert rep.files_indexed == 2 and rep.files_failed == 0          # 二期.md、白板.canvas；.obsidian、.trash 跳过
        note = str((v / "项目" / "二期.md").resolve())
        assert {(t["tag"], t["origin"]) for t in a.repo.tags_of(note)} == {("项目/二期", "file"), ("预算", "file"),
                                                                             ("待办", "file")}
        assert [f.name for f in keyword_search(a.repo, "最终预算").files] == ["二期.md"]
        assert not keyword_search(a.repo, "私下的备注").files
        assert [f.name for f in keyword_search(a.repo, "预算八十万").files] == ["白板.canvas"]
        # 改了笔记的标签：文件标签跟着换，手动加的保留
        fid = a.repo.get_file(note).id
        a.repo.add_tag(fid, "重要", "user")
        (v / "项目" / "二期.md").write_text(NOTE.replace("  - 预算\n", ""), encoding="utf-8")
        os.utime(v / "项目" / "二期.md", (time.time() + 5, time.time() + 5))
        a.indexer.index_folder(v)
        assert {t["tag"] for t in a.repo.tags_of(note)} == {"项目/二期", "待办", "重要"}
    finally:
        a.close()


def test_bridge_obsidian_vaults_and_open(tmp_path, monkeypatch):
    from lkos.ui.bridge import Bridge

    v = make_vault(tmp_path)
    other = tmp_path / "另一个"
    (other / ".obsidian").mkdir(parents=True)
    monkeypatch.setattr(obsidian, "discover", lambda path=None: [
        {"path": str(v), "name": v.name, "exists": True, "open": True, "ts": 2},
        {"path": str(other), "name": other.name, "exists": True, "open": False, "ts": 1},
        {"path": str(tmp_path / "没了"), "name": "没了", "exists": False, "open": False, "ts": 0}])
    monkeypatch.setattr(obsidian, "installed", lambda: True)
    opened, in_obsidian = [], []
    monkeypatch.setattr(obsidian, "open_in_obsidian", lambda p: in_obsidian.append(p))
    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False, opener=lambda p, reveal=False: opened.append(p))
    try:
        r = b.obsidian_vaults()
        assert [(x["name"], x["added"]) for x in r["vaults"]] == [("我的仓库", False), ("另一个", False)] and r["installed"]
        assert b.add_folder(str(v))["ok"]
        assert [x["added"] for x in b.obsidian_vaults()["vaults"]] == [True, False]
        assert b._call(b._sources)[0]["kind"] == "obsidian"
        note = str(v / "项目" / "二期.md")
        assert b.open_file(note)["ok"] and in_obsidian == [note] and not opened
        assert b.open_file(str(v / ".obsidian" / "app.json"))["ok"] and opened      # 非笔记文件照常用系统程序打开
        monkeypatch.setattr(obsidian, "installed", lambda: False)
        b.open_file(note)
        assert opened[-1] == note
    finally:
        b._close()


def test_survey(tmp_path):
    from lkos.config import DEFAULT_EXTENSIONS
    from lkos.indexing.survey import survey

    root = tmp_path / "资料"
    make_vault(root)
    (root / "a.pdf").write_bytes(b"%PDF")
    (root / "b.ppt").write_bytes(b"x")
    (root / "c.jpg").write_bytes(b"x" * 10)
    (root / "d.jpg").write_bytes(b"x")
    (root / "e.mobi").write_bytes(b"x")
    (root / "大文件.docx").write_bytes(b"x" * 2048)
    (root / ".隐藏.md").write_text("x")
    (root / ".文件.pdf.icloud").write_bytes(b"x")
    (root / "草稿").mkdir()
    (root / "草稿" / "f.md").write_text("x")
    (root / "Photos Library.photoslibrary").mkdir()
    (root / "Photos Library.photoslibrary" / "x.md").write_text("x")
    (root / "网页.html").write_text("<p>x</p>")
    (root / "网页_files").mkdir()
    (root / "网页_files" / "app.html").write_text("<p>资源</p>")
    r = survey(root, list(DEFAULT_EXTENSIONS), 1 / 1024, {"patterns": ["草稿"], "types": []})
    by = {t["ext"]: t for t in r["types"]}
    assert by["md"]["status"] == "ok" and by["md"]["indexable"] == 1          # 二期.md（草稿被排除、隐藏的不算）
    assert by["docx"]["too_large"] == 1 and "超过" in by["docx"]["hint"]
    assert by["ppt"]["status"] == "convert" and ".pptx" in by["ppt"]["hint"]
    assert by["jpg"]["status"] == "unsupported" and by["jpg"]["count"] == 2 and by["jpg"]["label"] == "图片"
    assert by["mobi"]["label"].startswith("电子书")
    assert by["html"]["count"] == 1 and by["canvas"]["indexable"] == 1
    assert r["vaults"] == [str((root / "我的仓库").resolve())]
    assert r["cloud_placeholders"] == 1 and r["hidden"] == 1
    assert {"Photos Library.photoslibrary", "网页_files"} <= set(r["skipped_dirs"])
    assert [t["status"] for t in r["types"]] == sorted((t["status"] for t in r["types"]),
                                                       key=["ok", "convert", "unsupported"].index)


def test_bridge_survey(tmp_path, corpus):
    from lkos.ui.bridge import Bridge

    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    try:
        assert b.survey()["error"]
        assert b.survey(str(tmp_path / "没有"))["error"]
        r = b.survey(str(corpus))["reports"][0]
        assert r["indexable"] == 4 and "source" not in r
        b.add_folder(str(corpus))
        r = b.survey()["reports"][0]
        assert r["source"]["kind"] == "folder" and {t["ext"] for t in r["types"]} >= {"pdf", "docx", "md", "xyz"}
    finally:
        b._close()
