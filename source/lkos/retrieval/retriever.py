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

from lkos.config import STRATEGIES, Config
from lkos.errors import ConfigError
from lkos.indexing.language import detect_lang
from lkos.indexing.tokenize import build_or_query
from lkos.retrieval.fusion import rrf
from lkos.retrieval.select import merge_adjacent, rank_documents, select_chunks
from lkos.storage.repository import Repository
from lkos.types import RetrievedChunk

MODES = ("soft", "strict", "all")
MIN_CHUNK_CHARS = 40   # 短于这个长度的片段（封面、页眉）候选足够时不送进模型


@dataclass
class SearchDebug:
    lang: str
    mode: str
    lang_filter: str | None
    fts_query: str
    vec_ids: list[int]
    fts_ids: list[int]
    boosted: list[int] = field(default_factory=list)
    strategy: str = "flat"
    doc_rank: list[tuple[int, float]] = field(default_factory=list)


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
               mode: str | None = None, strategy: str | None = None) -> list[RetrievedChunk]:
        """strategy：doc_first（默认，先选文档再选段落）| flat（v0.2：直接取融合分最高的 k 个块）。"""
        if self.marker is None:
            return self._search(query, k, lang, mode, strategy)
        with self.marker.active():
            return self._search(query, k, lang, mode, strategy)

    def neighbors(self, chunk: RetrievedChunk) -> list[RetrievedChunk]:
        """片段前后各一个相邻块（不含片段本身覆盖的块）。"""
        if chunk.file_id is None or chunk.ordinal is None:
            return []
        end = chunk.ordinal_end if chunk.ordinal_end is not None else chunk.ordinal
        return self.repo.neighbors(chunk.file_id, [o for o in (chunk.ordinal - 1, end + 1) if o >= 0])

    def _search(self, query: str, k: int | None, lang: str | None, mode: str | None,
                strategy: str | None) -> list[RetrievedChunk]:
        r = self.cfg.retrieval
        k = k or r.top_k
        strategy = strategy or r.strategy
        if strategy not in STRATEGIES:
            raise ConfigError(f"检索策略只能是 {' 或 '.join(STRATEGIES)}：{strategy!r}")
        n = max(r.candidates_per_method, k)
        qlang, mode, lang_filter = self.resolve(query, lang, mode)

        qvec = self.gateway.embed([query], task="embed_query")[0]
        vec_ids = [cid for cid, _ in self.repo.knn(qvec, n, lang_filter, candidates=r.vector_candidates)]
        fts_query = build_or_query(query, qlang)
        fts_ids = [cid for cid, _ in self.repo.fts_search(fts_query, n, lang_filter)] if fts_query else []

        fused = rrf([vec_ids, fts_ids], k=r.rrf_k)
        boost: dict[int, float] = {}
        if mode == "soft" and fused:
            langs = self.repo.langs_of([cid for cid, _ in fused])
            boost = {cid: r.same_lang_boost for cid, _ in fused if langs.get(cid) == qlang}
        debug = SearchDebug(qlang, mode, lang_filter, fts_query, vec_ids, fts_ids, list(boost), strategy)
        self.last_debug = debug

        scores = dict(fused)
        pool = {c.chunk_id: c for c in self.repo.load_chunks([cid for cid, _ in fused])}
        # 只有标题、页眉的极短片段（封面页“××课程”之类）几乎不含答案却常因关键词命中排在前面：
        # 候选足够时不让它们占名额
        substantial = [(cid, sc) for cid, sc in fused if cid in pool and len(pool[cid].text.strip()) >= MIN_CHUNK_CHARS]
        if len(substantial) >= k:
            fused = substantial

        if strategy == "flat":
            ranked = sorted(((cid, s * boost.get(cid, 1.0)) for cid, s in fused), key=lambda x: -x[1])[:k]
            chunks = [pool[cid] for cid, _ in ranked if cid in pool]
            for c, (_, sc) in zip(chunks, ranked):
                c.score = round(sc, 6)
            return chunks

        file_of = self.repo.files_of_chunks([cid for cid, _ in fused])
        doc_rank = rank_documents(fused, file_of, boost, top=r.doc_score_top)
        debug.doc_rank = doc_rank
        for c in pool.values():
            c.score = round(scores[c.chunk_id], 6)
        want = k
        while True:
            picked = select_chunks(fused, file_of, doc_rank, want, r.per_doc_max)
            chunks = [pool[cid] for cid in picked if cid in pool]
            if r.merge_adjacent:
                chunks = merge_adjacent(chunks)  # 按文档名次排列，文档内按原文顺序
            # 合并后片段变少时再多取几个块，直到凑够 k 个片段或候选用完
            if len(chunks) >= k or len(picked) < want:
                return chunks
            want += k - len(chunks)
