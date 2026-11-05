"""标签：解析、自动生成、手动增删、按标签限定范围、内容变化后重新生成、后台任务。"""

import time

from lkos.app import App
from lkos.gateway.fake import FakeProvider
from lkos.qa.tags import normalize_tag, parse_tags
from lkos.types import Scope
from tests.conftest import TEST_DIM, fake_overrides


def test_parse_tags():
    assert parse_tags("标签：项目管理、预算、 预算 ,会议纪要;  #招标\n5. 进度") == ["项目管理", "预算", "会议纪要", "招标", "进度"]
    assert parse_tags("Security, log retention, Security, 2024") == ["Security", "log retention"]
    assert normalize_tag("《项目计划》") == "项目计划" and normalize_tag("x" * 20) == "" and normalize_tag("123") == ""


class TagFake(FakeProvider):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen_vocab = False

    def chat(self, model, messages, opts, on_token=None):
        user = messages[-1]["content"]
        if "主题标签" in user or "topic tags" in user:
            self.answer_fn = (lambda m: "预算、项目管理、会议") if ("预算" in user or "会议" in user) else (lambda m: "Security, Policy")
            assert ("已有标签" in user or "existing tags" in user) == bool(self.seen_vocab)   # 第一份之后带上已有标签
            self.seen_vocab = True
        else:
            self.answer_fn = None
        return super().chat(model, messages, opts, on_token)


def open_app(tmp_path, provider=None):
    return App.open(home=tmp_path / "h", overrides=fake_overrides(),
                    providers={"test": provider or TagFake("test", dim=TEST_DIM)})


def test_tagger_and_scope(tmp_path, corpus):
    a = open_app(tmp_path)
    try:
        a.indexer.index_folder(corpus)
        sid = a.repo.list_sources()[0]["id"]
        assert a.repo.tag_progress(sid) == (0, 4)
        progress = []
        n = a.tagger.run(sid, on_progress=lambda i, t, p: progress.append((i, t)))
        assert n == 4 and progress[-1] == (4, 4) and a.repo.tag_progress(sid) == (4, 4)
        assert a.repo.files_to_tag(sid) == [] and a.tagger.run(sid) == 0           # 已生成过：不再调用模型
        tags = {t["tag"]: t["files"] for t in a.repo.all_tags()}
        assert tags["预算"] >= 2 and "Security" in tags
        assert a.repo.list_sources()[0]["tagged"] == 4
        # 按标签限定范围
        hits = a.retriever.search("policy retention 预算", k=8, scope=Scope(tags=["Security"]))
        assert hits and all(h.file_type == "md" for h in hits)
        # 手动标签：加、删，重新生成时保留
        path = str((corpus / "项目计划书.docx").resolve())
        f = a.repo.get_file(path)
        a.repo.add_tag(f.id, "重点项目")
        a.repo.add_tag(f.id, "预算")                       # 模型生成过的：改为手动
        assert {t["tag"]: t["origin"] for t in a.repo.tags_of(path)} == \
            {"重点项目": "user", "预算": "user", "项目管理": "model", "会议": "model"}
        a.repo.set_model_tags(f.id, ["新标签"])
        assert {t["tag"] for t in a.repo.tags_of(path)} == {"重点项目", "预算", "新标签"}
        a.repo.remove_tag(f.id, "重点项目")
        assert "重点项目" not in {t["tag"] for t in a.repo.tags_of(path)}
    finally:
        a.close()


def test_changed_file_is_retagged(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    md = docs / "说明.md"
    md.write_text("# 说明\n\n会议安排在周三，讨论预算。\n", encoding="utf-8")
    a = open_app(tmp_path)
    try:
        a.indexer.index_folder(docs)
        a.tagger.run()
        assert a.repo.files_to_tag() == []
        md.write_text("# 说明\n\n会议改到周五，讨论预算和进度。\n", encoding="utf-8")
        a.indexer.index_folder(docs)
        assert [f.path.rsplit("/", 1)[-1] for f in a.repo.files_to_tag()] == ["说明.md"]
        a.indexer.index_folder(docs)                        # 再次更新（内容没变）：不影响
        assert len(a.repo.files_to_tag()) == 1
        a.repo.remove_files([a.repo.get_file(str(md.resolve())).id])
        assert a.repo.all_tags() == []
    finally:
        a.close()


def test_bridge_tags(tmp_path, corpus):
    from lkos.ui.bridge import Bridge

    b = Bridge(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": TagFake("test", dim=TEST_DIM)},
               auto_refresh=False)
    try:
        assert b.settings()["auto_tags"] is True
        assert b.add_folder(str(corpus))["ok"]
        end = time.time() + 15                              # 索引完成后自动在后台生成标签
        while time.time() < end and not (b._indexer.state.get("last_tags") and b._indexer.state["state"] == "idle"):
            time.sleep(0.05)
        assert b._indexer.state["last_tags"]["tagged"] == 4
        assert {t["tag"] for t in b.tags()} >= {"预算", "Security"}
        path = str((corpus / "项目计划书.docx").resolve())
        assert b.add_tag(path, " ")["error"] and b.add_tag(str(corpus / "没有.pdf"), "x")["error"]
        r = b.add_tag(path, "#重点")
        assert {"tag": "重点", "origin": "user"} in r["tags"]
        assert all(t["tag"] != "重点" for t in b.remove_tag(path, "重点")["tags"])
        assert b.generate_tags(str(corpus))["ok"] in (True, False)
    finally:
        b._close()
