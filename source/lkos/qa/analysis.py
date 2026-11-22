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
from lkos.qa.history import conversation_lang, history_messages, messages_tokens, prior_chunks
from lkos.qa.prompt import LABELS, fit_context, format_source, lang_key, with_web_note
from lkos.types import Answer
from lkos.web.search import interleave, web_queries

TASK = "knowledge_qa"
PLAN_TOKENS = 160            # 本机模型的输出上限；在线模型一律用模型配置的“输出上限”（网关统一处理）
KEYWORD_TOKENS = 120
# 联网搜索：(每次结果数, 取正文的网页数, 每个网页最多多少字)
WEB_LOCAL = (3, 2, 600)
WEB_ONLINE = (5, 3, 2000)
WEB_CAP = {False: 3, True: 8}   # 最多送入多少个网页片段（本机 / 在线），不让网页挤掉本机资料
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


def _budget_note(result, what: str) -> dict:
    """短输出（拆方面、写关键词）的额度被思考用完时，给界面一句说明。"""
    if getattr(result, "out_of_budget", False):
        return {"note": f"模型的输出额度都用在了思考上，{what}"}
    return {}


class Analyzer:
    def __init__(self, cfg, retriever, gateway, marker=None, web=None):
        self.cfg = cfg
        self.retriever = retriever
        self.gateway = gateway
        self.marker = marker
        self.web = web        # 联网搜索（WebSearcher）；开关在界面上，随时可改

    def analyze(self, question: str, on_progress: Callable[[str, int, int], None] | None = None, on_context=None,
                on_token=None, cancelled: Callable[[], bool] | None = None, scope=None,
                model_override: str | None = None, history=None, pinned=None,
                confirm=None, on_thinking=None) -> tuple[Answer, list[str], dict]:
        """返回 (回答, 拆出的方面, {"keywords": 各语言关键词, "lang_hits": 各语言命中数})。
        on_progress(阶段, 已完成, 总数[, 详情])：plan / keywords / search / write。
        history：同一对话之前的轮次（list[Turn]）；回答语言默认中文，对话一直用英文 / 法文时用那种语言。
        pinned / confirm：同 QAService.ask（附加的文件、发往在线模型前的确认）。"""
        with (self.marker.active() if self.marker else nullcontext()):
            return self._run(question.strip(), on_progress or (lambda *a: None), on_context, on_token,
                             cancelled or (lambda: False), scope, model_override, history or [], pinned or [],
                             confirm, on_thinking)

    def _run(self, q, progress, on_context, on_token, cancelled, scope, model_override, turns=(), pinned=(),
             confirm=None, on_thinking=None):
        if not q:
            raise LkosError("请输入要分析的问题")
        key = conversation_lang([t.question for t in turns] + [q])
        P = dict(PROMPTS["zh" if key == "zh" else "en"])
        if key == "fr":
            P["system"] = P["system"].replace("Answer in English.", "Réponds en français.")
        ref = self.gateway.resolve(TASK, model_override)
        pc = self.cfg.providers.get(ref.split("/", 1)[0])
        online = bool(pc and pc.is_remote)
        per_aspect, per_lang = (PER_ASPECT * 2, PER_LANG * 2) if online else (PER_ASPECT, PER_LANG)
        short = dict(model_override=model_override, log_as="analysis", on_thinking=on_thinking, short=True)
        progress("plan", 0, 1)
        plan, _ = self.gateway.chat(TASK, [{"role": "user", "content": P["plan"].format(q=q)}],
                                    max_tokens=PLAN_TOKENS, **short)
        aspects = parse_aspects(plan.text) or [q]
        progress("plan", 1, 1, {"aspects": aspects, **_budget_note(plan, "没拆出方面，按整个问题检索")})
        if cancelled():
            raise Cancelled("已停止分析")

        progress("keywords", 0, 1)
        kw_out, _ = self.gateway.chat(TASK, [{"role": "user", "content": KEYWORD_PROMPT.format(q=q)}],
                                      max_tokens=KEYWORD_TOKENS, **short)
        keywords = parse_keywords(kw_out.text)
        progress("keywords", 1, 1, {"keywords": keywords, **_budget_note(kw_out, "没写出关键词")})

        extra = {"scope": scope} if scope is not None and not scope.empty else {}
        # 检索任务：每个方面（按提问语言）+ 每种语言的关键词（只在该语言的资料里找）
        base_q = q if not turns else f"{turns[-1].question}\n{q}"
        jobs = [("aspect", a, f"{base_q}\n{a}", "auto", per_aspect) for a in aspects]
        jobs += [("lang", lang, keywords[lang], lang, per_lang) for lang in LANGS if keywords.get(lang)]
        # 联网搜索：原问题 + （在线模型）关键词、前两个方面；开关在执行到这一步时才读，随时打开 / 关闭都对没做的检索生效
        if self.web is not None:
            jobs += [("web", w, w, "", 0) for w in web_queries(q, keywords, aspects, key, online)]
        groups, web_groups, lang_hits = [], [], {}
        wk_, wf, wc = WEB_ONLINE if online else WEB_LOCAL
        for i, (kind, label, query, lang, k) in enumerate(jobs, start=1):
            if cancelled():
                raise Cancelled("已停止分析")
            if kind == "web":
                if not self.web.enabled():
                    continue
                progress("search", i - 1, len(jobs), {"current": f"联网：{label}"})
                web_groups.append(self.web.chunks(query, k=wk_, fetch=wf, max_chars=wc, cancelled=cancelled))
                continue
            progress("search", i - 1, len(jobs), {"current": LANG_NAMES.get(label, label) if kind == "lang" else label})
            hits = self.retriever.search(query, k=k, lang=lang, **extra)
            groups.append(hits)
            if kind == "lang":
                lang_hits[label] = len(hits)
        web_pool = interleave(*web_groups)[:WEB_CAP[online]]     # 各次联网轮流取、去重、限量
        if web_pool:
            groups.append(web_pool)
        done = {"lang_hits": lang_hits}
        if self.web is not None and self.web.run.queries:
            done["web_pages"] = len(web_pool)
        progress("search", len(jobs), len(jobs), done)
        info = {"keywords": keywords, "lang_hits": lang_hits}

        # 轮流取每组的第 1、第 2……个片段，去重，保证每个方面、每种语言都有资料进入上下文
        ordered, seen = list(pinned), {c.chunk_id for c in pinned}
        for rank in range(max([per_aspect, per_lang] + [len(g) for g in groups])):
            for hits in groups:
                if rank < len(hits) and hits[rank].chunk_id not in seen:
                    seen.add(hits[rank].chunk_id)
                    ordered.append(hits[rank])
        if online:
            ordered += prior_chunks(list(turns), {c.chunk_id for c in ordered})
        if not ordered:
            return Answer(text={"zh": "资料中没有找到相关信息。", "fr": "Aucune information pertinente trouvée."}.get(
                key, "No relevant information was found."), no_context=True), aspects, info
        mcfg = self.cfg.chat_model(ref)
        num_ctx = mcfg.context_window
        out_tokens = mcfg.output_reserve() if online else ANALYSIS_TOKENS
        hist = history_messages(list(turns), online, num_ctx)
        aspect_text = "\n".join(f"{i}. {a}" for i, a in enumerate(aspects, start=1))
        context = fit_context(q + aspect_text + P["system"], ordered, lang_key(key), num_ctx - messages_tokens(hist),
                              out_tokens)
        if on_context:
            on_context(context)
        if online and confirm is not None:
            context = confirm(context)
        if cancelled():
            raise Cancelled("已停止分析")
        progress("write", 0, 1)
        src_label, _ = LABELS[lang_key(key)]
        sources = "\n\n".join(format_source(i, c) for i, c in enumerate(context, start=1))
        result, _ = self.gateway.chat(
            TASK, [{"role": "system", "content": with_web_note(P["system"], context, key)}, *hist,
                   {"role": "user", "content": P["user"].format(src_label=src_label, sources=sources, q=q,
                                                                aspects=aspect_text)}],
            model_override=model_override, on_token=on_token, max_tokens=out_tokens, log_as="analysis",
            on_thinking=on_thinking)
        self.gateway.ensure_answer(result, ref)
        text, citations, invalid = validate_citations(result.text, context)
        return Answer(text=text, citations=citations, invalid_refs=invalid, no_context=False, context=context,
                      ttft_ms=result.ttft_ms, latency_ms=result.latency_ms, input_tokens=result.input_tokens,
                      output_tokens=result.output_tokens), aspects, info
