"""混合检索：向量检索 + 关键词检索，RRF 融合，按问题语言处理文本块。

语言处理有三种模式（retrieval.language_filter）：
- soft（默认）：所有语言都参与检索，与问题同语言的文本块在融合时加权（same_lang_boost），
  适合中英混合的文档——换一种语言提问，另一种语言里真正相关的内容仍能被找到；
- strict：只检索与问题同语言的文本块（POC v0.1 的做法）；
- all：完全不区分语言。

实测依据见 docs/poc-results-2026-11-03-realdocs.md：硬过滤对同语言命中率没有提升，
却会把混合文档里另一种语言的答案排除在外。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lkos.config import Config
from lkos.errors import ConfigError
from lkos.indexing.language import detect_lang
from lkos.indexing.tokenize import build_or_query
from lkos.retrieval.fusion import rrf
from lkos.storage.repository import Repository
from lkos.types import RetrievedChunk

MODES = ("soft", "strict", "all")


@dataclass
class SearchDebug:
    lang: str
    mode: str
    lang_filter: str | None
    fts_query: str
    vec_ids: list[int]
    fts_ids: list[int]
    boosted: list[int] = field(default_factory=list)


def normalize_mode(mode: str | None) -> str:
    if mode in (None, ""):
        return "soft"
    if mode == "auto":  # v0.1 的 auto 等同于 strict
        return "strict"
    if mode not in MODES:
        raise ConfigError(f"语言模式只能是 {', '.join(MODES)}：{mode!r}")
    return mode


class Retriever:
    def __init__(self, cfg: Config, repo: Repository, gateway, marker=None):
        self.cfg = cfg
        self.repo = repo
        self.gateway = gateway
        self.marker = marker  # 问答活动标记：检索期间让后台索引暂停
        self.last_debug: SearchDebug | None = None

    def resolve(self, query: str, lang: str | None, mode: str | None) -> tuple[str, str, str | None]:
        """返回 (问题语言, 模式, 过滤语言)。

        lang：auto = 自动识别问题语言；all = 不区分语言（等同 mode=all）；zh/en/fr = 指定语言并严格过滤。
        """
        mode = normalize_mode(mode or self.cfg.retrieval.language_filter)
        if lang == "all":
            return detect_lang(query), "all", None
        if lang in (None, "auto"):
            qlang = detect_lang(query)
        else:
            qlang, mode = lang, "strict"
        if qlang == "other":
            return qlang, "all", None
        return qlang, mode, (qlang if mode == "strict" else None)

    def search(self, query: str, k: int | None = None, lang: str | None = "auto",
               mode: str | None = None) -> list[RetrievedChunk]:
        if self.marker is None:
            return self._search(query, k, lang, mode)
        with self.marker.active():
            return self._search(query, k, lang, mode)

    def _search(self, query: str, k: int | None, lang: str | None, mode: str | None) -> list[RetrievedChunk]:
        r = self.cfg.retrieval
        k = k or r.top_k
        n = max(r.candidates_per_method, k)
        qlang, mode, lang_filter = self.resolve(query, lang, mode)

        qvec = self.gateway.embed([query], task="embed_query")[0]
        vec_ids = [cid for cid, _ in self.repo.knn(qvec, n, lang_filter, candidates=r.vector_candidates)]
        fts_query = build_or_query(query, qlang)
        fts_ids = [cid for cid, _ in self.repo.fts_search(fts_query, n, lang_filter)] if fts_query else []

        fused = rrf([vec_ids, fts_ids], k=r.rrf_k)
        boosted: list[int] = []
        if mode == "soft" and fused:
            langs = self.repo.langs_of([cid for cid, _ in fused])
            out = []
            for cid, score in fused:
                if langs.get(cid) == qlang:
                    score *= r.same_lang_boost
                    boosted.append(cid)
                out.append((cid, score))
            fused = sorted(out, key=lambda x: -x[1])
        fused = fused[:k]
        self.last_debug = SearchDebug(qlang, mode, lang_filter, fts_query, vec_ids, fts_ids, boosted)

        chunks = self.repo.load_chunks([cid for cid, _ in fused])
        scores = dict(fused)
        for c in chunks:
            c.score = round(scores[c.chunk_id], 6)
        return chunks
