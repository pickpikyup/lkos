"""先选文档、再选段落（docs/m1-design.md 第 2 节）。都是纯函数，便于单独测试。

POC v0.2 直接取融合分最高的 k 个块：同一文档里与问题同语言、但不含答案的段落常把真正含答案的段落挤出去
（实测 5 题“文档找对了，答案段落没送进模型”）。这里分两步：
1. 文档排序：每个文档取最好的几个块的分数之和；soft 模式的同语言加权只作用在这一步；
2. 段落选择：按文档名次依次取，每个文档内按原始分数（不加权）取，最多 per_doc_max 个；
   最后同一文件相邻的块合并成一个片段，去掉切块时的重叠部分。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from lkos.types import Location, RetrievedChunk


def rank_documents(scores: Sequence[tuple[int, float]], file_of: dict[int, int],
                   boost: dict[int, float] | None = None, top: int = 3) -> list[tuple[int, float]]:
    """scores：(chunk_id, 融合分)；file_of：chunk_id → file_id；boost：chunk_id → 倍率（同语言加权）。
    返回按文档得分从高到低的 (file_id, 得分)。"""
    per_file: dict[int, list[float]] = {}
    for cid, score in scores:
        fid = file_of.get(cid)
        if fid is None:
            continue
        per_file.setdefault(fid, []).append(score * (boost or {}).get(cid, 1.0))
    ranked = [(fid, sum(sorted(vals, reverse=True)[:top])) for fid, vals in per_file.items()]
    first_seen = {fid: i for i, fid in enumerate(dict.fromkeys(file_of[c] for c, _ in scores if c in file_of))}
    ranked.sort(key=lambda x: (-x[1], first_seen[x[0]]))
    return ranked


def select_chunks(scores: Sequence[tuple[int, float]], file_of: dict[int, int],
                  doc_rank: Sequence[tuple[int, float]], k: int, per_doc_max: int) -> list[int]:
    """按文档名次依次取块，每个文档内按原始分数取，最多 per_doc_max 个，总数 k 个。"""
    by_file: dict[int, list[tuple[int, float]]] = {}
    for cid, score in scores:
        if cid in file_of:
            by_file.setdefault(file_of[cid], []).append((cid, score))
    out: list[int] = []
    for fid, _ in doc_rank:
        for cid, _ in sorted(by_file.get(fid, []), key=lambda x: -x[1])[:per_doc_max]:
            out.append(cid)
            if len(out) >= k:
                return out
    return out


def merge_texts(a: str, b: str, min_overlap: int = 8) -> str:
    """把相邻的两块拼起来：b 开头若与 a 结尾重复（切块重叠，至少 min_overlap 个字符），只保留一份。"""
    limit = min(len(a), len(b))
    for n in range(limit, min_overlap - 1, -1):
        if a.endswith(b[:n]):
            return a + b[n:]
    return a + "\n" + b


def _merge_location(a: Location, b: Location) -> Location:
    def lo(x, y):
        vals = [v for v in (x, y) if v is not None]
        return min(vals) if vals else None

    def hi(x, y):
        vals = [v for v in (x, y) if v is not None]
        return max(vals) if vals else None

    pages = [p for p in (a.page, a.page_end, b.page, b.page_end) if p is not None]
    page = min(pages) if pages else None
    page_end = max(pages) if pages else None
    heading = None
    if a.heading_path and b.heading_path:
        common = []
        for x, y in zip(a.heading_path.split(" > "), b.heading_path.split(" > ")):
            if x != y:
                break
            common.append(x)
        heading = " > ".join(common) or None
    return Location(page=page, page_end=page_end if page_end != page else None, heading_path=heading,
                    para_start=lo(a.para_start, b.para_start), para_end=hi(a.para_end, b.para_end),
                    line_start=lo(a.line_start, b.line_start), line_end=hi(a.line_end, b.line_end))


def merge_pair(a: RetrievedChunk, b: RetrievedChunk) -> RetrievedChunk:
    """合并同一文件中序号相邻的两块（a 在前）。保留 a 的 chunk_id，分数取较高者。"""
    return replace(a, text=merge_texts(a.text, b.text), location=_merge_location(a.location, b.location),
                   score=max(a.score, b.score), ordinal_end=b.ordinal_end if b.ordinal_end is not None else b.ordinal,
                   lang=a.lang if a.lang == b.lang else a.lang)


def _end(c: RetrievedChunk) -> int | None:
    return c.ordinal_end if c.ordinal_end is not None else c.ordinal


def merge_adjacent(chunks: Sequence[RetrievedChunk]) -> list[RetrievedChunk]:
    """同一文件中序号相邻（或重叠）的块合并为一个片段；结果保持各文件首次出现的顺序，文件内按原文顺序。"""
    groups: dict[int | str, list[RetrievedChunk]] = {}
    for c in chunks:
        groups.setdefault(c.file_id if c.file_id is not None else c.file_path, []).append(c)
    out: list[RetrievedChunk] = []
    for items in groups.values():
        if any(c.ordinal is None for c in items):
            out.extend(items)
            continue
        items = sorted(items, key=lambda c: c.ordinal)
        cur = items[0]
        for nxt in items[1:]:
            if nxt.ordinal <= _end(cur) + 1:
                if _end(nxt) > _end(cur):
                    cur = merge_pair(cur, nxt)
                else:
                    cur = replace(cur, score=max(cur.score, nxt.score))
            else:
                out.append(cur)
                cur = nxt
        out.append(cur)
    return out
