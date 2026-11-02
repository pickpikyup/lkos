"""混合检索：向量检索 + 关键词检索，RRF 融合；默认只匹配与问题同语言的文本块。"""

from __future__ import annotations

from dataclasses import dataclass

from lkos.config import Config
from lkos.indexing.language import detect_lang
from lkos.indexing.tokenize import build_or_query
from lkos.retrieval.fusion import rrf
from lkos.storage.repository import Repository
from lkos.types import RetrievedChunk


@dataclass
class SearchDebug:
    lang: str
    lang_filter: str | None
    fts_query: str
    vec_ids: list[int]
    fts_ids: list[int]


class Retriever:
    def __init__(self, cfg: Config, repo: Repository, gateway):
        self.cfg = cfg
        self.repo = repo
        self.gateway = gateway
        self.last_debug: SearchDebug | None = None

    def resolve_lang(self, query: str, lang: str | None) -> tuple[str, str | None]:
        """返回 (问题语言, 过滤语言)。过滤语言为 None 表示不过滤。"""
        if lang in (None, "auto"):
            lang = detect_lang(query)
            if self.cfg.retrieval.language_filter == "all":
                return lang, None
        if lang in ("all", "other"):
            return (detect_lang(query) if lang == "all" else lang), None
        return lang, lang

    def search(self, query: str, k: int | None = None, lang: str | None = "auto") -> list[RetrievedChunk]:
        k = k or self.cfg.retrieval.top_k
        n = max(self.cfg.retrieval.candidates_per_method, k)
        qlang, lang_filter = self.resolve_lang(query, lang)

        qvec = self.gateway.embed([query], task="embed_query")[0]
        vec_ids = [cid for cid, _ in self.repo.knn(qvec, n, lang_filter)]
        fts_query = build_or_query(query, qlang)
        fts_ids = [cid for cid, _ in self.repo.fts_search(fts_query, n, lang_filter)] if fts_query else []
        self.last_debug = SearchDebug(qlang, lang_filter, fts_query, vec_ids, fts_ids)

        fused = rrf([vec_ids, fts_ids], k=self.cfg.retrieval.rrf_k)[:k]
        chunks = self.repo.load_chunks([cid for cid, _ in fused])
        scores = dict(fused)
        for c in chunks:
            c.score = round(scores[c.chunk_id], 6)
        return chunks
