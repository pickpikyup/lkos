"""关键词检索：不理解语义、不用模型，按原文逐字查找（像“查找”一样）。

- 空格分开的多个词表示“都要出现”（在同一个文本块里）；用引号括起来的词组按整体查找，例如 "二期 预算"；
- 英文、法文不区分大小写；
- 结果按文件分组：目录、文件名、出现次数，以及每一处的位置（页 / 段落 / 行）和前后文。
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from lkos.types import Location

SNIPPET_CHARS = 60


@dataclass
class KeywordHit:
    chunk_id: int
    loc: str
    page: int | None
    snippet: str
    count: int


@dataclass
class KeywordFile:
    path: str
    file_type: str
    count: int = 0
    hits: list[KeywordHit] = field(default_factory=list)

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def folder(self) -> str:
        return str(Path(self.path).parent)


@dataclass
class KeywordResult:
    terms: list[str]
    files: list[KeywordFile]
    total_hits: int
    ms: int
    truncated: bool


def parse_terms(query: str) -> list[str]:
    """“二期 预算” → ["二期", "预算"]；引号里的词组保留空格。"""
    q = (query or "").replace("“", '"').replace("”", '"').replace("　", " ").strip()
    try:
        parts = shlex.split(q)
    except ValueError:
        parts = q.split()
    return list(dict.fromkeys(p.strip() for p in parts if p.strip()))[:8]


def _count(text: str, term: str) -> int:
    return len(re.findall(re.escape(term), text, flags=re.I))


def _snippet(text: str, terms: list[str]) -> str:
    flat = re.sub(r"\s+", " ", text)
    m = None
    for t in terms:
        m = re.search(re.escape(t), flat, flags=re.I)
        if m:
            break
    if not m:
        return flat[: SNIPPET_CHARS * 2]
    start, end = max(0, m.start() - SNIPPET_CHARS), min(len(flat), m.end() + SNIPPET_CHARS)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def keyword_search(repo, query: str, scope=None, max_files: int = 200, hits_per_file: int = 20) -> KeywordResult:
    t0 = time.perf_counter()
    terms = parse_terms(query)
    if not terms:
        return KeywordResult(terms=[], files=[], total_hits=0, ms=0, truncated=False)
    where, params = repo._scope_sql(scope)
    conds = " AND ".join("instr(lower(c.text), lower(?)) > 0" for _ in terms)
    sql = (f"SELECT c.id, c.text, c.page, c.heading_path, c.para_start, c.para_end, c.line_start, c.line_end, "
           f"fl.path, fl.file_type, fl.mtime FROM chunks c JOIN files fl ON fl.id = c.file_id "
           f"WHERE {conds}{where} ORDER BY fl.mtime DESC, fl.path, c.ordinal")
    files: dict[str, KeywordFile] = {}
    seen_locs: dict[str, set] = {}
    total = 0
    truncated = False
    for r in repo.conn.execute(sql, [*terms, *params]):
        f = files.get(r["path"])
        if f is None:
            if len(files) >= max_files:
                truncated = True
                break
            f = files[r["path"]] = KeywordFile(path=r["path"], file_type=r["file_type"])
            seen_locs[r["path"]] = set()
        loc = Location(page=r["page"], heading_path=r["heading_path"], para_start=r["para_start"],
                       para_end=r["para_end"], line_start=r["line_start"], line_end=r["line_end"]).describe(r["file_type"])
        n = sum(_count(r["text"], t) for t in terms)
        snip = _snippet(r["text"], terms)
        key = (loc, snip)
        if key in seen_locs[r["path"]]:      # 相邻文本块有重叠：同一处只算一次
            continue
        seen_locs[r["path"]].add(key)
        f.count += n
        total += n
        if len(f.hits) < hits_per_file:
            f.hits.append(KeywordHit(chunk_id=int(r["id"]), loc=loc, page=r["page"], snippet=snip, count=n))
    ordered = sorted(files.values(), key=lambda f: -f.count)
    return KeywordResult(terms=terms, files=ordered, total_hits=total, ms=int((time.perf_counter() - t0) * 1000),
                         truncated=truncated)


def result_dict(res: KeywordResult) -> dict:
    return {"terms": res.terms, "total_hits": res.total_hits, "ms": res.ms, "truncated": res.truncated,
            "files": [{"path": f.path, "name": f.name, "folder": f.folder, "type": f.file_type, "count": f.count,
                       "hits": [{"id": h.chunk_id, "loc": h.loc, "page": h.page, "snippet": h.snippet, "count": h.count}
                                for h in f.hits]} for f in res.files]}
