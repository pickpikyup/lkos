"""深度分析（完整设计文档第 8 节）：先把问题拆成 2–4 个方面，每个方面单独检索，再按方面逐一分析、综合结论。

为了 8 GB 内存下的速度，只用两次模型调用：
1. 拆分：列出要分析的几个方面（很短的输出）；
2. 分析：把各方面检索到的片段（每个方面至少一个，轮流装入上下文预算）一起交给模型，按方面写分析，每条结论标出处。
"""

from __future__ import annotations

import re
from contextlib import nullcontext
from typing import Callable

from lkos.errors import Cancelled, LkosError
from lkos.qa.citations import validate_citations
from lkos.qa.prompt import LABELS, fit_context, format_source, lang_key
from lkos.qa.service import answer_lang
from lkos.types import Answer

TASK = "knowledge_qa"
PLAN_TOKENS = 160
ANALYSIS_TOKENS = 900
PER_ASPECT = 3

PROMPTS = {
    "zh": {
        "plan": "要深入分析这个问题：{q}\n请把它拆成 2–4 个需要分别查资料的方面，每行一个，只写方面本身（简短的问句或名词短语），不要编号以外的解释。",
        "system": "你是分析助手。只根据下面编号的资料分析，不要编造。按给出的几个方面逐一分析：每个方面用“### 方面名”做小标题，"
                  "结论后用 [编号] 标注出处；资料不足以回答的方面，直接写“资料中没有提到”。最后写“### 结论”，综合各方面给出 2–4 句判断。用中文回答。",
        "user": "{src_label}\n{sources}\n\n问题：{q}\n请按这几个方面分析：\n{aspects}",
    },
    "en": {
        "plan": "To analyse this question in depth: {q}\nSplit it into 2–4 aspects that need separate research, one per line, "
                "short questions or noun phrases only, no explanation.",
        "system": "You are an analyst. Use only the numbered sources below; do not invent anything. Analyse each given aspect "
                  "under a \"### <aspect>\" heading and cite sources as [n]; if the sources do not cover an aspect, say so. "
                  "Finish with \"### Conclusion\": 2–4 sentences combining the aspects. Answer in English.",
        "user": "{src_label}\n{sources}\n\nQuestion: {q}\nAnalyse these aspects:\n{aspects}",
    },
}


def parse_aspects(text: str, limit: int = 4) -> list[str]:
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•·]|\d+[.、)）]|[一二三四五六七八九十]+[、.])\s*", "", line).strip(" ：:。")
        if 2 <= len(line) <= 60 and line not in out:
            out.append(line)
    return out[:limit]


class Analyzer:
    def __init__(self, cfg, retriever, gateway, marker=None):
        self.cfg = cfg
        self.retriever = retriever
        self.gateway = gateway
        self.marker = marker

    def analyze(self, question: str, on_progress: Callable[[str, int, int], None] | None = None, on_context=None,
                on_token=None, cancelled: Callable[[], bool] | None = None, scope=None,
                model_override: str | None = None) -> tuple[Answer, list[str]]:
        """返回 (回答, 拆出的方面)。on_progress(阶段, 已完成, 总数)：plan / search / write。"""
        with (self.marker.active() if self.marker else nullcontext()):
            return self._run(question.strip(), on_progress or (lambda *a: None), on_context, on_token,
                             cancelled or (lambda: False), scope, model_override)

    def _run(self, q, progress, on_context, on_token, cancelled, scope, model_override):
        if not q:
            raise LkosError("请输入要分析的问题")
        key = lang_key(answer_lang(q))
        P = PROMPTS["zh" if key == "zh" else "en"]
        progress("plan", 0, 1)
        plan, _ = self.gateway.chat(TASK, [{"role": "user", "content": P["plan"].format(q=q)}],
                                    model_override=model_override, max_tokens=PLAN_TOKENS, log_as="analysis")
        aspects = parse_aspects(plan.text) or [q]

        extra = {"scope": scope} if scope is not None and not scope.empty else {}
        per_aspect = []
        for i, a in enumerate(aspects, start=1):
            if cancelled():
                raise Cancelled("已停止分析")
            progress("search", i - 1, len(aspects))
            per_aspect.append(self.retriever.search(f"{q}\n{a}", k=PER_ASPECT, **extra))
        progress("search", len(aspects), len(aspects))

        # 轮流取每个方面的第 1、第 2……个片段，去重，保证每个方面都有资料进入上下文
        ordered, seen = [], set()
        for rank in range(PER_ASPECT):
            for hits in per_aspect:
                if rank < len(hits) and hits[rank].chunk_id not in seen:
                    seen.add(hits[rank].chunk_id)
                    ordered.append(hits[rank])
        if not ordered:
            return Answer(text="资料中没有找到相关信息。" if key == "zh" else "No relevant information was found.",
                          no_context=True), aspects
        num_ctx = self.cfg.chat_model(self.gateway.resolve(TASK, model_override)).context_window
        aspect_text = "\n".join(f"{i}. {a}" for i, a in enumerate(aspects, start=1))
        context = fit_context(q + aspect_text + P["system"], ordered, key, num_ctx, ANALYSIS_TOKENS)
        if on_context:
            on_context(context)
        if cancelled():
            raise Cancelled("已停止分析")
        progress("write", 0, 1)
        src_label, _ = LABELS[key]
        sources = "\n\n".join(format_source(i, c) for i, c in enumerate(context, start=1))
        result, _ = self.gateway.chat(
            TASK, [{"role": "system", "content": P["system"]},
                   {"role": "user", "content": P["user"].format(src_label=src_label, sources=sources, q=q,
                                                                aspects=aspect_text)}],
            model_override=model_override, on_token=on_token, max_tokens=ANALYSIS_TOKENS, log_as="analysis")
        text, citations, invalid = validate_citations(result.text, context)
        return Answer(text=text, citations=citations, invalid_refs=invalid, no_context=False, context=context,
                      ttft_ms=result.ttft_ms, latency_ms=result.latency_ms, input_tokens=result.input_tokens,
                      output_tokens=result.output_tokens), aspects
