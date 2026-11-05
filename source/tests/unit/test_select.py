"""先选文档、再选段落（docs/m1-design.md 第 2 节）。"""

import pytest

from lkos.app import App
from lkos.errors import ConfigError
from lkos.gateway.fake import FakeProvider
from lkos.qa.prompt import estimate_tokens
from lkos.qa.service import expand_best
from lkos.retrieval.select import merge_adjacent, merge_texts, rank_documents, select_chunks
from lkos.types import Location, RetrievedChunk
from tests.conftest import TEST_DIM, fake_overrides


def rc(cid, fid, ordinal, text="内容", score=0.0, page=None, heading=None, file_type="pdf"):
    return RetrievedChunk(chunk_id=cid, file_path=f"/d/f{fid}.{file_type}", file_type=file_type, text=text,
                          lang="zh", location=Location(page=page, heading_path=heading), score=score,
                          file_id=fid, ordinal=ordinal, ordinal_end=ordinal)


# ---------- 文档排序与段落选择 ----------
def test_rank_documents_sums_top_chunks_and_boost_only_ranks_documents():
    scores = [(1, 0.030), (2, 0.029), (3, 0.031), (4, 0.010)]
    file_of = {1: 10, 2: 10, 3: 20, 4: 10}
    assert [f for f, _ in rank_documents(scores, file_of, top=1)] == [20, 10]
    assert [f for f, _ in rank_documents(scores, file_of, top=3)] == [10, 20]
    # 同语言加权让文件 20 的块翻倍，文档名次随之改变
    assert [f for f, _ in rank_documents(scores, file_of, boost={3: 3.0}, top=3)] == [20, 10]


def test_select_chunks_prefers_raw_score_within_document():
    """POC 中文问 vCPU 失败的情形：同文档里的中文段落（加权后更高）挤掉了含答案的英文表格。"""
    scores = [(1, 0.030), (2, 0.029), (3, 0.032)]       # 3 = 英文表格，原始分数最高
    file_of = {1: 10, 2: 10, 3: 10}
    boost = {1: 1.2, 2: 1.2}                            # 1、2 与问题同语言
    flat = sorted(((c, s * boost.get(c, 1)) for c, s in scores), key=lambda x: -x[1])[:2]
    assert 3 not in [c for c, _ in flat]
    rank = rank_documents(scores, file_of, boost)
    assert select_chunks(scores, file_of, rank, k=2, per_doc_max=3) == [3, 1]


def test_select_chunks_per_doc_limit_and_k():
    scores = [(1, .9), (2, .8), (3, .7), (4, .6), (5, .5)]
    file_of = {1: 10, 2: 10, 3: 10, 4: 20, 5: 20}
    rank = rank_documents(scores, file_of)
    assert select_chunks(scores, file_of, rank, k=4, per_doc_max=2) == [1, 2, 4, 5]
    assert select_chunks(scores, file_of, rank, k=3, per_doc_max=3) == [1, 2, 3]
    assert select_chunks(scores, {}, [], k=3, per_doc_max=3) == []


# ---------- 合并相邻片段 ----------
@pytest.mark.parametrize("a,b,out", [
    ("一二三甲乙丙丁戊己庚辛壬癸", "甲乙丙丁戊己庚辛壬癸子丑寅", "一二三甲乙丙丁戊己庚辛壬癸子丑寅"),  # 重叠只保留一份
    ("第一段内容", "第二段内容", "第一段内容\n第二段内容"),                   # 没有重叠
    ("结尾是的", "的开头", "结尾是的\n的开头"),                               # 少于 8 个字符的巧合不算重叠
])
def test_merge_texts(a, b, out):
    assert merge_texts(a, b) == out


def test_merge_adjacent_merges_consecutive_ordinals_only():
    chunks = [rc(1, 10, 5, "C", .1, page=3), rc(2, 20, 0, "X", .5), rc(3, 10, 4, "B", .3, page=2),
              rc(4, 10, 9, "Z", .2, page=7)]
    out = merge_adjacent(chunks)
    assert [(c.file_id, c.ordinal, c.ordinal_end) for c in out] == [(10, 4, 5), (10, 9, 9), (20, 0, 0)]
    merged = out[0]
    assert merged.text == "B\nC" and merged.score == .3 and merged.chunk_id == 3
    assert merged.location.page == 2 and merged.location.page_end == 3
    assert merged.describe() == "f10.pdf · 第 2–3 页"


def test_merge_adjacent_keeps_common_heading_and_absorbs_covered_chunk():
    a = rc(1, 10, 0, "A" * 20, .5, heading="手册 > 第一章 > 1.1", file_type="docx")
    b = rc(2, 10, 1, "B" * 20, .4, heading="手册 > 第一章 > 1.2", file_type="docx")
    out = merge_adjacent([a, b])
    assert len(out) == 1 and out[0].location.heading_path == "手册 > 第一章"
    covered = rc(3, 10, 1, "B" * 20, .9)
    again = merge_adjacent([out[0], covered])
    assert len(again) == 1 and again[0].score == .9 and again[0].ordinal_end == 1


# ---------- 补邻 ----------
def _neighbors(table):
    return lambda chunk: [c for c in table if c.file_id == chunk.file_id
                          and c.ordinal in (chunk.ordinal - 1, chunk.ordinal_end + 1)]


def test_expand_best_adds_neighbors_when_best_clearly_leads():
    best, other = rc(1, 10, 3, "答案在这里", .03), rc(2, 20, 0, "别的", .01)
    table = [rc(7, 10, 2, "前文"), rc(8, 10, 4, "后文")]
    out = expand_best("问题", [best, other], "zh", 4096, 512, _neighbors(table), ratio=1.5)
    assert out[0].text == "前文\n答案在这里\n后文" and (out[0].ordinal, out[0].ordinal_end) == (2, 4)
    assert out[1] is other


def test_expand_best_skips_when_scores_close_or_disabled_or_over_budget():
    best, other = rc(1, 10, 3, "答案", .03), rc(2, 20, 0, "别的", .025)
    table = [rc(7, 10, 2, "前文" * 500)]
    nb = _neighbors(table)
    ctx = [best, other]
    assert expand_best("问题", ctx, "zh", 4096, 512, nb, ratio=1.5) == ctx        # 分数接近
    assert expand_best("问题", [best], "zh", 4096, 512, nb, ratio=0) == [best]     # 关闭
    assert expand_best("问题", [best], "zh", 4096, 512, None, ratio=1.5) == [best]
    assert estimate_tokens(table[0].text) > 900
    assert expand_best("问题", [best], "zh", 1600, 512, nb, ratio=1.5) == [best]   # 超出预算不补
    grown = expand_best("问题", [best], "zh", 4096, 512, nb, ratio=1.5)             # 只有一个片段时直接补
    assert grown[0].ordinal == 2


# ---------- 与检索器集成 ----------
@pytest.fixture
def make_app(tmp_path, corpus):
    apps = []

    def _make(**retrieval):
        a = App.open(home=tmp_path / f"h{len(apps)}", overrides=fake_overrides(retrieval=retrieval),
                     providers={"test": FakeProvider("test", dim=TEST_DIM)})
        a.indexer.index_folder(corpus)
        apps.append(a)
        return a

    yield _make
    for a in apps:
        a.close()


def test_doc_first_groups_by_document_and_records_ranking(make_app):
    a = make_app(strategy="doc_first")
    hits = a.retriever.search("项目二期的预算上限是多少？", k=4)
    dbg = a.retriever.last_debug
    assert dbg.strategy == "doc_first" and dbg.doc_rank
    files = [h.file_id for h in hits]
    assert files == sorted(files, key=[f for f, _ in dbg.doc_rank].index)   # 按文档名次排列
    for fid in set(files):                                                # 文档内按原文顺序
        ords = [h.ordinal for h in hits if h.file_id == fid]
        assert ords == sorted(ords)


def test_per_doc_max_one_gives_distinct_documents(make_app):
    a = make_app(per_doc_max=1, merge_adjacent=False, strategy="doc_first")
    hits = a.retriever.search("预算 budget", k=3)
    assert len({h.file_id for h in hits}) == len(hits) == 3


def test_flat_strategy_and_invalid_strategy(make_app):
    a = make_app()
    flat = a.retriever.search("预算", k=3, strategy="flat")
    assert a.retriever.last_debug.strategy == "flat"
    assert [h.score for h in flat] == sorted((h.score for h in flat), reverse=True)
    with pytest.raises(ConfigError):
        a.retriever.search("预算", strategy="magic")


def test_neighbors_returns_chunks_around(make_app):
    a = make_app()
    rows = a.repo.conn.execute("SELECT file_id, ordinal FROM chunks WHERE ordinal = 1 LIMIT 1").fetchone()
    mid = a.repo.neighbors(rows["file_id"], [1])[0]
    around = a.retriever.neighbors(mid)
    assert [c.ordinal for c in around] == [0, 2][:len(around)] and all(c.file_id == mid.file_id for c in around)
    assert a.retriever.neighbors(RetrievedChunk(1, "/x", "md", "t", "zh", Location())) == []


def test_config_rejects_bad_retrieval_options():
    from lkos.config import load_config

    for bad in ({"strategy": "x"}, {"per_doc_max": 0}, {"neighbor_ratio": -1}):
        with pytest.raises(ConfigError):
            load_config(None, overrides={"retrieval": bad})


def test_tiny_title_chunks_do_not_take_slots(tmp_path):
    """封面页只有标题的片段（关键词命中排名靠前）在候选足够时不送进模型。"""
    docs = tmp_path / "d"
    docs.mkdir()
    (docs / "课程.md").write_text("# 思想领导力课程\n\n## 封面\n\n思想领导力课程\n\n## 定义\n\n"
                                + "思想领导力是一些观点和想法的集合，通过深刻的洞察力引领他人思考、激发变革。" * 2
                                + "\n\n## 案例\n\n" + "公司持续推出思想领导力产品，推动产业发展，促进做大市场空间。" * 2 + "\n",
                                encoding="utf-8")
    (docs / "其他.md").write_text("# 其他\n\n" + "这是一段与思想领导力无关的较长说明文字，用来凑足候选数量。" * 3 + "\n",
                                encoding="utf-8")
    a = App.open(home=tmp_path / "h", overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        a.indexer.index_folder(docs)
        hits = a.retriever.search("思想领导力", k=2)
        assert hits and all(len(h.text.strip()) >= 40 for h in hits)
    finally:
        a.close()
