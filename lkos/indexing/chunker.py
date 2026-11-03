"""切块：把解析出的 Block 合并为适合检索与 4K 上下文的文本块（完整设计文档第 5 节、docs/m1-design.md）。

规则：
- 同一页、同一个前 boundary_depth 级标题下的相邻 Block 合并，长度不超过 max_chars；
- 长度按语言区分（M1）：每个“页 + 标题”段先判断主要语言，中文默认 500 字、其他语言 1,200 字符，
  重叠为长度的 15%；两种语言的片段 token 数因此接近；
- 不跨页、不跨前两级标题（默认 2：Markdown 常把一级标题当文档标题，只按一级切会让整篇合成一块，
  引用定位过粗）；
- 因长度切开时，下一块以上一块末尾 overlap_chars 个字符开头；
- 超长 Block 按句切开，仍超长则硬切。
"""

from __future__ import annotations

import re
from typing import Callable, Iterable

from lkos.indexing.language import detect_lang
from lkos.types import Block, Chunk, Location

_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])|(?<=[.!?])\s+")
_CJK_ANY = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")


def _top_heading(loc: Location, depth: int) -> tuple[str, ...]:
    return tuple(loc.heading_path.split(" > ")[:depth]) if loc.heading_path else ()


def _common_heading(paths: list[str | None]) -> str | None:
    parts = [p.split(" > ") for p in paths if p]
    if not parts or len(parts) != len(paths):
        return None
    common: list[str] = []
    for items in zip(*parts):
        if all(x == items[0] for x in items):
            common.append(items[0])
        else:
            break
    return " > ".join(common) or None


def _min(vals):
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def _max(vals):
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def merge_locations(locs: list[Location]) -> Location:
    return Location(
        page=locs[0].page,
        heading_path=_common_heading([l.heading_path for l in locs]),
        para_start=_min(l.para_start for l in locs),
        para_end=_max(l.para_end for l in locs),
        line_start=_min(l.line_start for l in locs),
        line_end=_max(l.line_end for l in locs),
    )


def hard_split(text: str, max_chars: int) -> list[str]:
    """按长度硬切；拉丁文字尽量在空格处切，避免切断单词。"""
    out: list[str] = []
    rest = text.strip()
    while len(rest) > max_chars:
        cut = max_chars
        if not _CJK_ANY.search(rest[max_chars - 1:max_chars + 1]):
            space = rest.rfind(" ", 0, max_chars + 1)
            if space > max_chars // 2:
                cut = space
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out


def split_sentences(text: str, max_chars: int) -> list[str]:
    pieces = [p.strip() for p in _SENTENCE_END.split(text) if p and p.strip()]
    out: list[str] = []
    cur = ""
    for p in pieces:
        if len(p) > max_chars:
            if cur:
                out.append(cur.strip())
                cur = ""
            out.extend(hard_split(p, max_chars))
            continue
        sep = "" if (not cur or _CJK_ANY.search(cur[-1]) or _CJK_ANY.search(p[0])) else " "
        if len(cur) + len(sep) + len(p) > max_chars and cur:
            out.append(cur.strip())
            cur = p
        else:
            cur = cur + sep + p
    if cur.strip():
        out.append(cur.strip())
    return [o for o in out if o]


DEFAULT_BY_LANG = {"zh": 500, "other": 1200}
_SAMPLE_CHARS = 3000


class Chunker:
    """max_chars / overlap_chars：所有语言同一长度（v0.2 行为，用于对照）。
    max_chars_by_lang + overlap_ratio：按段落主要语言选长度（M1 默认）。
    """

    def __init__(self, max_chars: int = 800, overlap_chars: int = 100,
                 lang_detector: Callable[[str], str] = detect_lang, boundary_depth: int = 2,
                 max_chars_by_lang: dict[str, int] | None = None, overlap_ratio: float | None = None):
        if max_chars_by_lang is not None:
            if "other" not in max_chars_by_lang:
                raise ValueError("max_chars_by_lang 必须包含 other")
            if overlap_ratio is None or not 0 <= overlap_ratio < 1:
                raise ValueError("overlap_ratio 必须在 0 到 1 之间")
            if min(max_chars_by_lang.values()) < 20:
                raise ValueError("切块长度不能小于 20")
        elif overlap_chars >= max_chars:
            raise ValueError("overlap_chars 必须小于 max_chars")
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.by_lang = dict(max_chars_by_lang) if max_chars_by_lang else None
        self.overlap_ratio = overlap_ratio
        self.detect = lang_detector
        self.boundary_depth = boundary_depth

    def signature(self) -> str:
        """切块参数签名：参数变化后，索引器据此重新切块。"""
        if self.by_lang:
            sizes = "-".join(f"{k}{v}" for k, v in sorted(self.by_lang.items()))
            return f"{sizes}-ov{self.overlap_ratio:g}-d{self.boundary_depth}"
        return f"all{self.max_chars}-ov{self.overlap_chars}c-d{self.boundary_depth}"

    def limits_for(self, text: str) -> tuple[int, int]:
        """返回 (长度上限, 重叠长度)。"""
        if not self.by_lang:
            return self.max_chars, self.overlap_chars
        lang = self.detect(text[:_SAMPLE_CHARS])
        size = self.by_lang.get(lang, self.by_lang["other"])
        return size, min(int(round(size * self.overlap_ratio)), size - 1)

    def _segments(self, blocks: Iterable[Block]) -> list[list[Block]]:
        segs: list[list[Block]] = []
        for block in blocks:
            if segs:
                prev = segs[-1][-1].location
                if (block.location.page == prev.page
                        and _top_heading(block.location, self.boundary_depth)
                        == _top_heading(prev, self.boundary_depth)):
                    segs[-1].append(block)
                    continue
            segs.append([block])
        return segs

    def chunk(self, blocks: Iterable[Block]) -> list[Chunk]:
        raw: list[tuple[str, Location]] = []
        for seg in self._segments(blocks):
            max_chars, overlap = self.limits_for("\n".join(b.text for b in seg))
            raw.extend(_pack(seg, max_chars, overlap))
        return [Chunk(ordinal=i, text=t, lang=self.detect(t), location=loc) for i, (t, loc) in enumerate(raw)]


def _pack(blocks: list[Block], max_chars: int, overlap_chars: int) -> list[tuple[str, Location]]:
    """把同一段（同页、同标题）的 Block 合并成不超过 max_chars 的块。"""
    raw: list[tuple[str, Location]] = []
    buf: list[Block] = []

    def size(items: list[Block]) -> int:
        return sum(len(b.text) for b in items) + max(len(items) - 1, 0)

    def flush(with_overlap: bool) -> None:
        nonlocal buf
        if not buf:
            return
        text = "\n".join(b.text for b in buf)
        raw.append((text, merge_locations([b.location for b in buf])))
        last = buf[-1]
        buf = []
        if with_overlap and overlap_chars > 0:
            tail = text[-overlap_chars:].lstrip()
            if tail:
                buf = [Block(text=tail, location=last.location)]

    for block in blocks:
        if buf and size(buf) + 1 + len(block.text) > max_chars:
            flush(with_overlap=True)
        if len(block.text) > max_chars:
            if len(buf) == 1 and raw and raw[-1][0].endswith(buf[0].text):
                buf = []  # 只剩上一块的重叠尾巴，不单独成块
            flush(with_overlap=False)
            for piece in split_sentences(block.text, max_chars):
                raw.append((piece, block.location))
            continue
        buf.append(block)
    flush(with_overlap=False)
    return raw
