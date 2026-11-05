"""问答流程编排：混合检索（先选文档再选段落）→ 按预算装入片段 → 补入最佳片段的相邻块 → 生成 → 校验引用。"""

from __future__ import annotations

import re

from lkos.config import Config
from lkos.gateway.base import TokenCallback
from lkos.indexing.language import detect_lang
from lkos.qa.citations import validate_citations
from lkos.qa.prompt import NO_CONTEXT_MESSAGE, build_messages, fit_context, history_tokens, lang_key
from lkos.retrieval.select import merge_adjacent
from lkos.types import Answer, RetrievedChunk

TASK = "knowledge_qa"
_LATIN_WORD = re.compile(r"[A-Za-zÀ-ÿ]{2,}")
_CJK = re.compile(r"[\u3400-\u9fff]")


def answer_lang(question: str, cfg=None) -> str:
    """回答用的语言。完整的英文 / 法文句子按句子语言回答；只有一两个英文词（如“AI”“5G FWA”）、
    没有汉字的短输入，按默认语言（中文）回答——这类输入多半是中文用户在输关键词。"""
    if not _CJK.search(question) and len(_LATIN_WORD.findall(question)) < 3:
        return "zh"
    return detect_lang(question)


class QAService:
    def __init__(self, cfg: Config, retriever, gateway, marker=None):
        self.cfg = cfg
        self.retriever = retriever
        self.gateway = gateway
        self.marker = marker  # 问答活动标记：检索 + 生成期间让后台索引暂停

    def ask(self, question: str, k: int | None = None, model_override: str | None = None,
            lang: str | None = "auto", on_token: TokenCallback | None = None,
            mode: str | None = None, strategy: str | None = None, on_context=None, scope=None,
            history: list[tuple[str, str]] | None = None) -> Answer:
        """on_context(片段列表)：片段选好、开始生成之前调用，界面据此先显示找到的资料。"""
        if self.marker is None:
            return self._ask(question, k, model_override, lang, on_token, mode, strategy, on_context, scope, history)
        with self.marker.active():
            return self._ask(question, k, model_override, lang, on_token, mode, strategy, on_context, scope, history)

    def _ask(self, question, k, model_override, lang, on_token, mode, strategy, on_context=None, scope=None,
             history=None) -> Answer:
        question = question.strip()
        qlang = answer_lang(question, self.cfg)
        extra = {}
        if strategy is not None:
            extra["strategy"] = strategy
        if scope is not None and not scope.empty:
            extra["scope"] = scope
        # 追问：检索时把上一轮的问题带上（“那第二点呢”这类问题本身缺少关键词）
        query = question if not history else f"{history[-1][0]}\n{question}"
        hits = self.retriever.search(query, k=k, lang=lang, mode=mode, **extra)
        if not hits:
            return Answer(text=NO_CONTEXT_MESSAGE[lang_key(qlang)], no_context=True)

        ref = self.gateway.resolve(TASK, model_override)
        num_ctx = self.cfg.chat_model(ref).context_window
        task = self.cfg.tasks[TASK]
        context = fit_context(question, hits, qlang, num_ctx - history_tokens(history), task.max_tokens)
        if not context:
            return Answer(text=NO_CONTEXT_MESSAGE[lang_key(qlang)], no_context=True)
        if (strategy or self.cfg.retrieval.strategy) == "doc_first":
            context = expand_best(question, context, qlang, num_ctx, task.max_tokens,
                                  getattr(self.retriever, "neighbors", None), self.cfg.retrieval.neighbor_ratio)

        if on_context is not None:
            on_context(context)
        messages = build_messages(question, context, qlang, history=history)
        result, _ = self.gateway.chat(TASK, messages, model_override=model_override, on_token=on_token)
        text, citations, invalid = validate_citations(result.text, context)
        return Answer(
            text=text, citations=citations, invalid_refs=invalid, no_context=False, context=context,
            ttft_ms=result.ttft_ms, latency_ms=result.latency_ms,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        )


def expand_best(question: str, context: list[RetrievedChunk], lang: str, num_ctx: int, max_output: int,
                neighbors, ratio: float) -> list[RetrievedChunk]:
    """最佳片段明显领先（分数 ≥ 第二名 × ratio，或只有一个片段）时，把它前后各一个相邻块合并进来，
    减少答案被切在块边界上的情况；补入后超出上下文预算就不补。ratio = 0 或没有 neighbors 时不做。"""
    if not context or not ratio or neighbors is None:
        return context
    order = sorted(range(len(context)), key=lambda i: -context[i].score)
    best_i = order[0]
    best = context[best_i]
    if len(order) > 1 and best.score < ratio * context[order[1]].score:
        return context
    extra = [n for n in neighbors(best) if n.chunk_id not in {c.chunk_id for c in context}]
    if not extra:
        return context
    for n in extra:
        n.score = 0.0
    grown = merge_adjacent([*context[:best_i], best, *extra, *context[best_i + 1:]])
    fitted = fit_context(question, grown, lang, num_ctx, max_output)
    return grown if len(fitted) == len(grown) else context
