"""问答流程编排：混合检索（先选文档再选段落）→ 按预算装入片段 → 补入最佳片段的相邻块 → 生成 → 校验引用。"""

from __future__ import annotations

import re

from lkos.config import Config
from lkos.gateway.base import TokenCallback
from lkos.indexing.language import detect_lang
from lkos.qa.citations import validate_citations
from lkos.qa.history import Turn, history_messages, messages_tokens, prior_chunks
from lkos.qa.prompt import LABELS, NO_CONTEXT_MESSAGE, build_messages, fit_context, format_source, lang_key
from lkos.retrieval.select import merge_adjacent
from lkos.types import Answer, RetrievedChunk
from lkos.web.search import clean_query, interleave

TASK = "knowledge_qa"
_LATIN_WORD = re.compile(r"[A-Za-zÀ-ÿ]{2,}")
_CJK = re.compile(r"[\u3400-\u9fff]")


def answer_lang(question: str, cfg=None) -> str:
    """回答用的语言。完整的英文 / 法文句子按句子语言回答；只有一两个英文词（如“AI”“5G FWA”）、
    没有汉字的短输入，按默认语言（中文）回答——这类输入多半是中文用户在输关键词。"""
    if not _CJK.search(question) and len(_LATIN_WORD.findall(question)) < 3:
        return "zh"
    return detect_lang(question)


CHAT_MAX_TOKENS = 1024
CHAT_SYSTEM = {
    "zh": "你是乐于助人的助手。回答准确、简洁、有条理；不确定的事情直接说不确定，不要编造。用中文回答。",
    "en": "You are a helpful assistant. Be accurate, concise and well organised; say so when you are not sure. Answer in English.",
    "fr": "Tu es un assistant serviable. Sois précis, concis et structuré ; dis-le si tu n'es pas sûr. Réponds en français.",
}
ONLINE_TOP_K = 12   # 在线模型一次检索的片段数（本机模型按 retrieval.top_k）
# 联网搜索：(结果数, 取正文的网页数, 每个网页最多多少字)；本机模型上下文小，少取一些
WEB_LOCAL = (3, 2, 600)
WEB_ONLINE = (6, 4, 2000)
# 对话模式打开联网时追加的说明
CHAT_WEB_NOTE = {
    "zh": "\n下面附有联网搜索到的网页（编号资料）。回答时优先依据这些网页，引用处用 [编号] 标注；"
          "网页没有涉及、靠你自己知识补充的部分要说明；网页说法不一致时都列出。",
    "en": "\nNumbered web pages from a live web search are attached. Prefer them, cite as [n], say which parts come "
          "from your own knowledge, and list conflicting claims.",
    "fr": "\nDes pages web numérotées issues d'une recherche sont jointes. Appuie-toi d'abord sur elles, cite-les "
          "avec [n], indique ce qui vient de tes propres connaissances et signale les contradictions.",
}


def web_query(question: str, turns) -> str:
    """联网搜索用的查询：问题去掉指令词（查找、核实、联网……）和问句词；“那第二点呢”这类很短的追问带上上一轮的问题。"""
    q = clean_query(question) or question.strip()
    if turns and len(q) < 8:
        q = f"{clean_query(turns[-1].question)} {q}".strip()
    return q[:200]


class QAService:
    def __init__(self, cfg: Config, retriever, gateway, marker=None, web=None):
        self.cfg = cfg
        self.retriever = retriever
        self.gateway = gateway
        self.marker = marker  # 问答活动标记：检索 + 生成期间让后台索引暂停
        self.web = web        # 联网搜索（WebSearcher），开关在界面上，随时可改

    def _web_chunks(self, question: str, turns, online: bool) -> list:
        if self.web is None or not self.web.enabled():
            return []
        k, fetch, chars = WEB_ONLINE if online else WEB_LOCAL
        return self.web.chunks(web_query(question, turns), k=k, fetch=fetch, max_chars=chars)

    def ask(self, question: str, k: int | None = None, model_override: str | None = None,
            lang: str | None = "auto", on_token: TokenCallback | None = None,
            mode: str | None = None, strategy: str | None = None, on_context=None, scope=None,
            history=None, pinned=None, confirm=None, on_thinking=None) -> Answer:
        """on_context(片段列表)：片段选好、开始生成之前调用，界面据此先显示找到的资料。
        history：同一对话之前的轮次（list[Turn]，旧写法 [(问, 答)] 也可以）；
        pinned：用户附加的整份文件的片段（只给在线模型，排在最前）；
        confirm(片段列表) → 片段列表：发给在线模型前让用户确认（可以去掉一些），返回空列表或抛 Cancelled 表示不发。"""
        if self.marker is None:
            return self._ask(question, k, model_override, lang, on_token, mode, strategy, on_context, scope, history,
                             pinned, confirm, on_thinking)
        with self.marker.active():
            return self._ask(question, k, model_override, lang, on_token, mode, strategy, on_context, scope, history,
                             pinned, confirm, on_thinking)

    def chat(self, question: str, model_override: str | None = None, history=None,
             on_token: TokenCallback | None = None, on_thinking=None, on_context=None, confirm=None) -> Answer:
        """纯对话：不查资料库，不用向量和关键词检索，直接和模型对话（带上同一对话之前的轮次）。
        打开联网搜索时，先搜网页、作为编号资料附上（模型引用时标 [编号]）。"""
        question = question.strip()
        ref = self.gateway.resolve(TASK, model_override)
        online = self.is_online(ref)
        mcfg = self.cfg.chat_model(ref)
        turns = [t if isinstance(t, Turn) else Turn(question=t[0], answer=t[1]) for t in (history or [])]
        max_out = mcfg.output_reserve() if online else CHAT_MAX_TOKENS
        hist = history_messages(turns, online, mcfg.context_window - max_out, chat=True)
        lang = answer_lang(question, self.cfg)
        system = CHAT_SYSTEM.get(lang, CHAT_SYSTEM["en"])
        context = self._web_chunks(question, turns, online)
        if context:
            context = fit_context(question, context, lang, mcfg.context_window - messages_tokens(hist), max_out)
            if on_context is not None:
                on_context(context)
            if online and confirm is not None:
                context = confirm(context)
        user = question
        if context:
            system += CHAT_WEB_NOTE.get(lang, CHAT_WEB_NOTE["en"])
            src_label, q_label = LABELS[lang_key(lang)]
            sources = "\n\n".join(format_source(i, c) for i, c in enumerate(context, start=1))
            user = f"{src_label}\n{sources}\n\n{q_label}{question}"
        messages = [{"role": "system", "content": system}, *hist, {"role": "user", "content": user}]
        kw = dict(model_override=model_override, on_token=on_token, max_tokens=max_out, log_as="chat",
                  on_thinking=on_thinking)
        if self.marker is None:
            result, _ = self.gateway.chat(TASK, messages, **kw)
        else:
            with self.marker.active():
                result, _ = self.gateway.chat(TASK, messages, **kw)
        self.gateway.ensure_answer(result, ref)
        text, citations, invalid = (validate_citations(result.text, context) if context
                                    else (result.text.strip(), [], []))
        return Answer(text=text.strip(), citations=citations, invalid_refs=invalid, no_context=False, context=context,
                      ttft_ms=result.ttft_ms, latency_ms=result.latency_ms, input_tokens=result.input_tokens,
                      output_tokens=result.output_tokens)

    def is_online(self, ref: str) -> bool:
        pc = self.cfg.providers.get(ref.split("/", 1)[0])
        return bool(pc and pc.is_remote)

    def _ask(self, question, k, model_override, lang, on_token, mode, strategy, on_context=None, scope=None,
             history=None, pinned=None, confirm=None, on_thinking=None) -> Answer:
        question = question.strip()
        qlang = answer_lang(question, self.cfg)
        turns = [t if isinstance(t, Turn) else Turn(question=t[0], answer=t[1]) for t in (history or [])]
        ref = self.gateway.resolve(TASK, model_override)
        online = self.is_online(ref)
        mcfg = self.cfg.chat_model(ref)
        num_ctx = mcfg.context_window
        task = self.cfg.tasks[TASK]
        max_out = mcfg.output_reserve() if online else task.max_tokens
        extra = {}
        if strategy is not None:
            extra["strategy"] = strategy
        if scope is not None and not scope.empty:
            extra["scope"] = scope
        # 追问：检索时把上一轮的问题带上（“那第二点呢”这类问题本身缺少关键词）
        query = question if not turns else f"{turns[-1].question}\n{question}"
        hits = self.retriever.search(query, k=k or (ONLINE_TOP_K if online else None), lang=lang, mode=mode, **extra)
        hist = history_messages(turns, online, num_ctx)
        web = self._web_chunks(question, turns, online)
        local = [h for h in hits if h.chunk_id not in {p.chunk_id for p in pinned or []}]
        pool = list(pinned or []) + (interleave(local, web) if web else local)
        if online:   # 在线模型上下文大：之前各轮找到的资料也一起带上
            pool += prior_chunks(turns, {c.chunk_id for c in pool})
        if not pool:
            return Answer(text=NO_CONTEXT_MESSAGE[lang_key(qlang)], no_context=True)
        context = fit_context(question, pool, qlang, num_ctx - messages_tokens(hist), max_out)
        if not context:
            return Answer(text=NO_CONTEXT_MESSAGE[lang_key(qlang)], no_context=True)
        if not online and (strategy or self.cfg.retrieval.strategy) == "doc_first":
            context = expand_best(question, context, qlang, num_ctx, task.max_tokens,
                                  getattr(self.retriever, "neighbors", None), self.cfg.retrieval.neighbor_ratio)
        if on_context is not None:
            on_context(context)
        if online and confirm is not None:
            context = confirm(context)
            if not context and not turns:
                return Answer(text=NO_CONTEXT_MESSAGE[lang_key(qlang)], no_context=True)
        messages = build_messages(question, context, qlang, history=hist)
        result, _ = self.gateway.chat(TASK, messages, model_override=model_override, on_token=on_token,
                                      on_thinking=on_thinking)
        self.gateway.ensure_answer(result, ref)
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
    if best.file_type == "web":          # 网页没有“相邻块”
        return context
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
