"""深度分析（完整设计文档第 8 节）：先把问题拆成 2–4 个方面，每个方面单独检索，再按方面逐一分析、综合结论。

三次模型调用（8 GB 内存下控制总时长）：
1. 拆分：列出要分析的几个方面（很短的输出）；
2. 关键词：把问题的关键词写成中文、英文、法文三种（很短的输出），每种语言只在该语言的资料里检索，
   保证三种语言的资料都有机会进入上下文（普通问答只按提问语言加权）；
3. 分析：各方面和各语言检索到的片段轮流装入上下文预算，按方面写分析，每条结论标出处。
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
KEYWORD_TOKENS = 120
ANALYSIS_TOKENS = 900
PER_ASPECT = 3
PER_LANG = 3
LANGS = ("zh", "en", "fr")
LANG_NAMES = {"zh": "中文", "en": "English", "fr": "Français"}

KEYWORD_PROMPT = ("下面是一个要在资料库里检索的问题：{q}\n"
                  "请提取 3–6 个检索关键词（专有名词、术语、核心概念），分别写成中文、英文、法文，严格按三行输出，不要解释：\n"
                  "中文：关键词1 关键词2 …\nEnglish: keyword1 keyword2 …\nFrançais: mot-clé1 mot-clé2 …")
_KW_LINE = {
    "zh": re.compile(r"^\s*(?:中文|汉语|chinese|zh)\s*[:：]\s*(.+)$", re.I),
    "en": re.compile(r"^\s*(?:english|英文|英语|en)\s*[:：]\s*(.+)$", re.I),
    "fr": re.compile(r"^\s*(?:fran[cç]ais|french|法文|法语|fr)\s*[:：]\s*(.+)$", re.I),
}


def parse_keywords(text: str) -> dict[str, str]:
    """模型输出 → {"zh": "...", "en": "...", "fr": "..."}；缺的语言不出现。"""
    out = {}
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.、)）])\s*", "", line)
        for lang, rx in _KW_LINE.items():
            m = rx.match(line)
            if m and lang not in out:
                kw = re.sub(r"[，,、;；]+", " ", m.group(1)).strip(" 。.…")
                kw = re.sub(r"\s+", " ", kw.replace("…", " ")).strip()
                if kw:
                    out[lang] = kw[:200]
    return out

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
                model_override: str | None = None) -> tuple[Answer, list[str], dict]:
        """返回 (回答, 拆出的方面, {"keywords": 各语言关键词, "lang_hits": 各语言命中数})。
        on_progress(阶段, 已完成, 总数[, 详情])：plan / keywords / search / write。"""
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
        progress("plan", 1, 1, {"aspects": aspects})
        if cancelled():
            raise Cancelled("已停止分析")

        progress("keywords", 0, 1)
        kw_out, _ = self.gateway.chat(TASK, [{"role": "user", "content": KEYWORD_PROMPT.format(q=q)}],
                                      model_override=model_override, max_tokens=KEYWORD_TOKENS, log_as="analysis")
        keywords = parse_keywords(kw_out.text)
        progress("keywords", 1, 1, {"keywords": keywords})

        extra = {"scope": scope} if scope is not None and not scope.empty else {}
        # 检索任务：每个方面（按提问语言）+ 每种语言的关键词（只在该语言的资料里找）
        jobs = [("aspect", a, f"{q}\n{a}", "auto", PER_ASPECT) for a in aspects]
        jobs += [("lang", lang, keywords[lang], lang, PER_LANG) for lang in LANGS if keywords.get(lang)]
        groups, lang_hits = [], {}
        for i, (kind, label, query, lang, k) in enumerate(jobs, start=1):
            if cancelled():
                raise Cancelled("已停止分析")
            progress("search", i - 1, len(jobs), {"current": LANG_NAMES.get(label, label) if kind == "lang" else label})
            hits = self.retriever.search(query, k=k, lang=lang, **extra)
            groups.append(hits)
            if kind == "lang":
                lang_hits[label] = len(hits)
        progress("search", len(jobs), len(jobs), {"lang_hits": lang_hits})
        info = {"keywords": keywords, "lang_hits": lang_hits}

        # 轮流取每组的第 1、第 2……个片段，去重，保证每个方面、每种语言都有资料进入上下文
        ordered, seen = [], set()
        for rank in range(max(PER_ASPECT, PER_LANG)):
            for hits in groups:
                if rank < len(hits) and hits[rank].chunk_id not in seen:
                    seen.add(hits[rank].chunk_id)
                    ordered.append(hits[rank])
        if not ordered:
            return Answer(text="资料中没有找到相关信息。" if key == "zh" else "No relevant information was found.",
                          no_context=True), aspects, info
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
                      output_tokens=result.output_tokens), aspects, info
