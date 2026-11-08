"""文件夹在访达里改名 / 移动：自动认出、手动重新定位、重复添加时合并、旧出处照样能打开。"""

import os
import time

from lkos.app import App
from lkos.gateway.fake import FakeProvider
from lkos.indexing.relocate import folder_ident, relocate_missing, resolve_path
from lkos.retrieval.keyword import keyword_search
from tests.conftest import TEST_DIM, fake_overrides
from tests.samples import build_corpus


def open_app(tmp_path):
    return App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})


def test_auto_detect_rename_and_move(tmp_path):
    base = tmp_path / "文稿"
    corpus = base / "工作资料"
    build_corpus(corpus)
    a = open_app(tmp_path)
    try:
        a.indexer.index_folder(corpus)
        src = a.repo.list_sources()[0]
        assert src["ident"] == folder_ident(corpus)
        sid, n = src["id"], src["files"]
        a.repo.update_source_settings(sid, refresh="daily")
        old_file = a.repo.files_of_source(sid)[0].path
        # 改名
        renamed = base / "工作资料2026"
        corpus.rename(renamed)
        assert relocate_missing(a.repo) == [{"old": str(corpus.resolve()), "new": str(renamed.resolve())}]
        s = a.repo.list_sources()[0]
        assert s["id"] == sid and s["path"] == str(renamed.resolve()) and s["files"] == n and s["refresh"] == "daily"
        assert all(f.path.startswith(str(renamed.resolve()) + os.sep) for f in a.repo.files_of_source(sid))
        assert resolve_path(a.repo, old_file) == old_file.replace("工作资料", "工作资料2026", 1) or \
            os.path.exists(resolve_path(a.repo, old_file))
        rep = a.indexer.index_folder(renamed)
        assert rep.files_indexed == 0 and rep.files_moved == 0        # 没变的文件不重新解析
        # 移到旁边的子文件夹里，同时上级也改了名
        (base / "归档").mkdir()
        renamed.rename(base / "归档" / "工作资料2026")
        base.rename(tmp_path / "我的文稿")
        moved = relocate_missing(a.repo)
        final = (tmp_path / "我的文稿" / "归档" / "工作资料2026").resolve()
        assert moved and a.repo.list_sources()[0]["path"] == str(final)
        assert os.path.exists(resolve_path(a.repo, old_file))           # 改过两次名，旧出处也能接上
        assert keyword_search(a.repo, "一百二十万").files
        # 什么都没变：不做事
        assert relocate_missing(a.repo) == []
    finally:
        a.close()


def test_not_found_and_lookalike_folder(tmp_path):
    corpus = tmp_path / "资料"
    build_corpus(corpus)
    a = open_app(tmp_path)
    try:
        a.indexer.index_folder(corpus)
        corpus.rename(tmp_path / "x")
        (tmp_path / "x").rename(tmp_path / "资料备份")
        os.makedirs(tmp_path / "空的")
        # 换一个身份相同但内容不对的情形不好造；这里检查：被移到很远的地方时认不出来，保持原样
        far = tmp_path / "a" / "b" / "c" / "d" / "e"
        far.mkdir(parents=True)
        (tmp_path / "资料备份").rename(far / "资料")
        assert relocate_missing(a.repo) == []
        assert a.repo.list_sources()[0]["path"] == str(corpus.resolve())
    finally:
        a.close()


def test_bridge_relocate_and_readd(tmp_path):
    from lkos.ui.bridge import Bridge

    base = tmp_path / "文稿"
    corpus = base / "资料"
    build_corpus(corpus)
    opened = []
    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False, opener=lambda p, reveal=False: opened.append(p))

    def wait_idle():
        end = time.time() + 10
        while time.time() < end and not (b._indexer.state["state"] == "idle" and b._indexer.state.get("last")):
            time.sleep(0.05)

    try:
        assert b.add_folder(str(corpus))["ok"]
        wait_idle()
        sid = b.library()["sources"][0]["id"]
        old_file = b._call(lambda: b._app.repo.files_of_source(sid))[0].path
        # 移到很远的地方：认不出来，显示“找不到”
        far = tmp_path / "x" / "y" / "z" / "w"
        far.mkdir(parents=True)
        corpus.rename(far / "资料")
        lib = b.library()
        assert lib["sources"][0]["missing"] and not lib["relocated"]
        # 选错文件夹：拒绝
        other = tmp_path / "别的"
        other.mkdir()
        (other / "a.md").write_text("# x\n\n别的内容", encoding="utf-8")
        assert "不是同一个文件夹" in b.relocate_to(sid, str(other))["error"]
        # 再次“添加”这个文件夹：认出是同一个，更新位置而不是新增
        r = b.add_folder(str(far / "资料"))
        assert r["ok"] and r["relocated"] == str(corpus.resolve())
        lib = b.library()
        assert len(lib["sources"]) == 1 and not lib["sources"][0]["missing"] and lib["sources"][0]["id"] == sid
        # 旧对话里的出处：用旧路径也能打开
        assert b.open_file(old_file)["ok"] and opened[-1].startswith(str((far / "资料").resolve()))
        # 改名后又被单独添加了一次：下次打开资料库时把旧的一项合并掉
        wait_idle()
        (far / "资料").rename(far / "资料2")
        b._call(lambda: b._app.repo.upsert_source(str((far / "资料2").resolve())))
        lib = b.library()
        assert [s["path"] for s in lib["sources"]] == [str((far / "资料2").resolve())] and lib["relocated"]
    finally:
        b._close()
