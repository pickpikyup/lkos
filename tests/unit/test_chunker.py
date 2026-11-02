import pytest

from lkos.indexing.chunker import Chunker, merge_locations, split_sentences
from lkos.types import Block, Location


def blk(text, **loc):
    return Block(text=text, location=Location(**loc))


def fixed_lang(_):
    return "zh"


def test_adjacent_blocks_on_same_page_merge():
    ch = Chunker(100, 10, fixed_lang).chunk([blk("甲" * 30, page=1), blk("乙" * 30, page=1)])
    assert len(ch) == 1
    assert ch[0].text == "甲" * 30 + "\n" + "乙" * 30
    assert ch[0].location.page == 1


def test_never_crosses_page():
    ch = Chunker(1000, 10, fixed_lang).chunk([blk("a", page=1), blk("b", page=2)])
    assert [c.location.page for c in ch] == [1, 2]
    assert [c.text for c in ch] == ["a", "b"]  # 跨页切开时不带重叠


def test_heading_boundaries_default_depth_two():
    blocks = [
        blk("w", heading_path="A > A1 > a", para_start=1, para_end=1),
        blk("x", heading_path="A > A1 > b", para_start=2, para_end=2),
        blk("y", heading_path="A > A2", para_start=4, para_end=4),
        blk("z", heading_path="B", para_start=6, para_end=6),
    ]
    ch = Chunker(1000, 10, fixed_lang).chunk(blocks)
    # 三级标题之间合并；二级、一级标题变化处切开
    assert [c.text for c in ch] == ["w\nx", "y", "z"]
    assert ch[0].location == Location(heading_path="A > A1", para_start=1, para_end=2)
    assert ch[2].location.heading_path == "B"


def test_boundary_depth_one_merges_subheadings():
    blocks = [blk("x", heading_path="A > A1"), blk("y", heading_path="A > A2"), blk("z", heading_path="B")]
    ch = Chunker(1000, 10, fixed_lang, boundary_depth=1).chunk(blocks)
    assert [c.text for c in ch] == ["x\ny", "z"]
    assert ch[0].location.heading_path == "A"


def test_size_limit_splits_with_overlap():
    blocks = [blk("a" * 60, line_start=1, line_end=1), blk("b" * 60, line_start=3, line_end=3)]
    ch = Chunker(100, 10, fixed_lang).chunk(blocks)
    assert len(ch) == 2
    assert ch[0].text == "a" * 60
    assert ch[1].text.startswith("a" * 10) and ch[1].text.endswith("b" * 60)
    # 重叠部分来自第 1 行，所以第二块的行号范围从第 1 行开始
    assert (ch[1].location.line_start, ch[1].location.line_end) == (1, 3)


def test_long_block_split_by_sentence():
    text = "第一句很长。" * 30  # 180 字
    ch = Chunker(50, 10, fixed_lang).chunk([blk(text, page=3)])
    assert len(ch) > 1
    assert all(len(c.text) <= 50 for c in ch)
    assert "".join(c.text for c in ch) == text
    assert all(c.location.page == 3 for c in ch)


def test_long_block_after_size_split_does_not_emit_overlap_alone():
    blocks = [blk("a" * 60), blk("b" * 60), blk("c" * 200)]
    ch = Chunker(100, 10, fixed_lang).chunk(blocks)
    assert not any(len(c.text) == 10 for c in ch)


def test_ordinals_and_lang_detection():
    en = "The budget cap for the second phase is one million and two hundred thousand."
    fr = "Le budget de la deuxième phase est plafonné à un million deux cent mille."
    ch = Chunker(200, 20).chunk([blk("预算上限一百万", page=1), blk(en, page=2), blk(fr, page=3)])
    assert [c.ordinal for c in ch] == [0, 1, 2]
    assert [c.lang for c in ch] == ["zh", "en", "fr"]


def test_hard_split_keeps_words_whole():
    ch = Chunker(20, 5, fixed_lang).chunk([blk("The budget cap is one million dollars", page=1)])
    assert all(len(c.text) <= 20 for c in ch)
    words = " ".join(c.text for c in ch).split()
    assert words == "The budget cap is one million dollars".split()


def test_empty_input():
    assert Chunker(100, 10).chunk([]) == []


def test_invalid_overlap():
    with pytest.raises(ValueError):
        Chunker(10, 10)


def test_split_sentences_hard_cut_and_english():
    assert split_sentences("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]
    out = split_sentences("One two. Three four! Five six?", 12)
    assert out == ["One two.", "Three four!", "Five six?"]


def test_merge_locations_common_prefix():
    loc = merge_locations([
        Location(heading_path="A > B > C", line_start=5, line_end=6),
        Location(heading_path="A > B > D", line_start=8, line_end=9),
    ])
    assert loc == Location(heading_path="A > B", line_start=5, line_end=9)
    assert merge_locations([Location(heading_path="A"), Location()]).heading_path is None
