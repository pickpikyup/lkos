"""问答流程编排：同语言混合检索 → 按预算装入片段 → 生成 → 校验引用。"""

from __future__ import annotations

from lkos.config import Config
from lkos.gateway.base import TokenCallback
from lkos.indexing.language import detect_lang
from lkos.qa.citations import validate_citations
from lkos.qa.prompt import NO_CONTEXT_MESSAGE, build_messages, fit_context, lang_key
from lkos.types import Answer

TASK = "knowledge_qa"


class QAService:
    def __init__(self, cfg: Config, retriever, gateway):
        self.cfg = cfg
        self.retriever = retriever
        self.gateway = gateway

    def ask(self, question: str, k: int | None = None, model_override: str | None = None,
            lang: str | None = "auto", on_token: TokenCallback | None = None,
            mode: str | None = None) -> Answer:
        question = question.strip()
        qlang = detect_lang(question)
        hits = self.retriever.search(question, k=k, lang=lang, mode=mode)
        if not hits:
            return Answer(text=NO_CONTEXT_MESSAGE[lang_key(qlang)], no_context=True)

        ref = self.gateway.resolve(TASK, model_override)
        num_ctx = self.cfg.chat_model(ref).context_window
        task = self.cfg.tasks[TASK]
        context = fit_context(question, hits, qlang, num_ctx, task.max_tokens)
        if not context:
            return Answer(text=NO_CONTEXT_MESSAGE[lang_key(qlang)], no_context=True)

        messages = build_messages(question, context, qlang)
        result, _ = self.gateway.chat(TASK, messages, model_override=model_override, on_token=on_token)
        text, citations, invalid = validate_citations(result.text, context)
        return Answer(
            text=text, citations=citations, invalid_refs=invalid, no_context=False, context=context,
            ttft_ms=result.ttft_ms, latency_ms=result.latency_ms,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        )
