"""切块：把解析出的 Block 合并为适合检索与 8K 上下文的文本块（设计文档第 9 节）。

规则：
- 同一页、同一个前 boundary_depth 级标题下的相邻 Block 合并，长度不超过 max_chars；
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


class Chunker:
    def __init__(self, max_chars: int = 800, overlap_chars: int = 100,
                 lang_detector: Callable[[str], str] = detect_lang, boundary_depth: int = 2):
        if overlap_chars >= max_chars:
            raise ValueError("overlap_chars 必须小于 max_chars")
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.detect = lang_detector
        self.boundary_depth = boundary_depth

    def chunk(self, blocks: Iterable[Block]) -> list[Chunk]:
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
            if with_overlap and self.overlap_chars > 0:
                tail = text[-self.overlap_chars:].lstrip()
                if tail:
                    buf = [Block(text=tail, location=last.location)]

        for block in blocks:
            if buf:
                prev = buf[-1].location
                boundary = (block.location.page != prev.page
                            or _top_heading(block.location, self.boundary_depth)
                            != _top_heading(prev, self.boundary_depth))
                if boundary:
                    flush(with_overlap=False)
                elif size(buf) + 1 + len(block.text) > self.max_chars:
                    flush(with_overlap=True)
            if len(block.text) > self.max_chars:
                if len(buf) == 1 and raw and raw[-1][0].endswith(buf[0].text):
                    buf = []  # 只剩上一块的重叠尾巴，不单独成块
                flush(with_overlap=False)
                for piece in split_sentences(block.text, self.max_chars):
                    raw.append((piece, block.location))
                continue
            buf.append(block)
        flush(with_overlap=False)

        return [Chunk(ordinal=i, text=t, lang=self.detect(t), location=loc) for i, (t, loc) in enumerate(raw)]
