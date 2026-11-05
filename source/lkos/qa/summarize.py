"""文档总结：整份文件的带出处摘要（完整设计文档第 8 节“文档总结”）。

4K 上下文放不下长文档，分两步：
1. 分段提炼：按原文顺序把文本块拼成若干“部分”，每部分不超过一次调用的预算，逐部分让模型写 3–6 条要点；
2. 汇总：把各部分要点按编号交给模型写全文总结，每条结论用 [编号] 标出来自哪一部分；
   部分太多、要点本身也放不下时，先把相邻部分的要点再合并一轮。
短文档一次就能放下时，直接用原文写总结。界面上的“出处”是各部分的原文（带页码 / 幻灯片 / 段落范围）。
"""

from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, replace
from functools import reduce
from typing import Callable

from lkos.errors import Cancelled, LkosError
from lkos.qa.citations import validate_citations
from lkos.qa.prompt import OUTPUT_MARGIN, estimate_tokens, lang_key
from lkos.retrieval.select import merge_pair
from lkos.types import Answer, RetrievedChunk

TASK = "knowledge_qa"          # 用问答的模型与温度；输出上限单独设置
MAP_TOKENS = 320               # 每部分要点的输出上限
FINAL_TOKENS = 700             # 全文总结的输出上限

PROMPTS = {
    "zh": {
        "system_map": "你是文档总结助手。只根据给出的内容写要点，不要编造，不要补充原文没有的信息。",
        "map": "下面是《{name}》{loc}的内容：\n\n{text}\n\n请用 3–6 条要点概括这部分的关键信息"
               "（结论、数字、决定、时间、人物或机构），每条一行，以“- ”开头，只写原文里有的内容。",
        "system_final": "你是文档总结助手。只根据下面编号的材料写总结，不要编造。"
                        "每条内容后用 [编号] 标注来自哪一部分，只能使用列出的编号。用中文回答。",
        "final": "文件：《{name}》\n\n{label}：\n{sources}\n\n请写一份全文总结：先用 2–3 句话说明这份文件讲什么，"
                 "然后分点列出最重要的 5–10 条内容（保留关键数字），每条后面标注 [编号]。",
        "label_points": "各部分要点", "label_text": "各部分原文",
    },
    "en": {
        "system_map": "You summarise documents. Write key points only from the given text; do not invent anything.",
        "map": "Below is the content of \"{name}\" {loc}:\n\n{text}\n\nSummarise the key information of this part "
               "(conclusions, figures, decisions, dates, people or organisations) in 3–6 bullet points, "
               "one per line starting with \"- \". Only use what the text says.",
        "system_final": "You summarise documents. Write the summary only from the numbered material below; do not "
                        "invent anything. After each point cite its part as [n], using only the listed numbers. "
                        "Answer in English.",
        "final": "Document: \"{name}\"\n\n{label}:\n{sources}\n\nWrite a summary of the whole document: start with "
                 "2–3 sentences on what it is about, then list the 5–10 most important points (keep key figures), "
                 "each followed by its [n].",
        "label_points": "Key points of each part", "label_text": "Text of each part",
    },
}


@dataclass
class Part:
    chunk: RetrievedChunk      # 合并后的原文与位置
    points: str = ""           # 分段提炼出的要点


def _lang(chunks: list[RetrievedChunk]) -> str:
    counts = Counter(c.lang for c in chunks)
    return "zh" if counts.get("zh", 0) >= sum(v for k, v in counts.items() if k != "zh") else "en"


def _pack(items: list, cost: Callable, budget: int) -> list[list]:
    """按原文顺序把条目装成若干组，每组的 cost 之和不超过 budget（单个超长条目独占一组）。"""
    groups: list[list] = []
    cur: list = []
    used = 0
    for it in items:
        c = cost(it)
        if cur and used + c > budget:
            groups.append(cur)
            cur, used = [], 0
        cur.append(it)
        used += c
    if cur:
        groups.append(cur)
    return groups


class Summarizer:
    def __init__(self, cfg, repo, gateway, marker=None):
        self.cfg = cfg
        self.repo = repo
        self.gateway = gateway
        self.marker = marker

    def _num_ctx(self, model_override: str | None) -> int:
        return self.cfg.chat_model(self.gateway.resolve(TASK, model_override)).context_window

    def summarize_file(self, path: str, on_progress: Callable[[str, int, int], None] | None = None,
                       on_token=None, on_context=None, cancelled: Callable[[], bool] | None = None,
                       model_override: str | None = None) -> Answer:
        """on_progress(阶段, 已完成, 总数)：阶段为 map（分段提炼）/ final（汇总）。"""
        with (self.marker.active() if self.marker else nullcontext()):
            return self._run(path, on_progress, on_token, on_context, cancelled or (lambda: False), model_override)

    def _chat(self, system: str, user: str, max_tokens: int, model_override, on_token=None):
        result, _ = self.gateway.chat(TASK, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                      model_override=model_override, on_token=on_token, max_tokens=max_tokens,
                                      log_as="summarize")
        return result

    def _run(self, path, on_progress, on_token, on_context, cancelled, model_override) -> Answer:
        chunks = self.repo.chunks_of_file(path)
        if not chunks:
            raise LkosError("这个文件还没有索引，或者没有可读的文字")
        name = chunks[0].file_name
        key = lang_key(_lang(chunks))
        P = PROMPTS["zh" if key == "zh" else "en"]
        num_ctx = self._num_ctx(model_override)
        progress = on_progress or (lambda *a: None)

        def cost(c: RetrievedChunk) -> int:
            return estimate_tokens(c.text) + 8

        # 汇总一次调用能放下的材料量
        final_budget = (num_ctx - FINAL_TOKENS - OUTPUT_MARGIN - estimate_tokens(P["system_final"] + P["final"])
                        - estimate_tokens(name) - 60)
        total = sum(cost(c) for c in chunks)

        if total <= final_budget:   # 短文档：直接用原文，分成最多 8 个部分作为出处
            per = max(final_budget // 8, total // 8 + 1)
            parts = [Part(reduce(merge_pair, g)) for g in _pack(chunks, cost, per)]
            label = P["label_text"]
            material = [p.chunk.text for p in parts]
        else:
            map_budget = (num_ctx - MAP_TOKENS - OUTPUT_MARGIN - estimate_tokens(P["system_map"] + P["map"])
                          - estimate_tokens(name) - 60)
            parts = [Part(reduce(merge_pair, g)) for g in _pack(chunks, cost, map_budget)]
            if on_context:
                on_context([p.chunk for p in parts])
            for i, part in enumerate(parts, start=1):
                if cancelled():
                    raise Cancelled("已停止总结")
                progress("map", i - 1, len(parts))
                loc = part.chunk.location.describe(part.chunk.file_type)
                part.points = self._chat(P["system_map"], P["map"].format(name=name, loc=f"（{loc}）" if loc else "",
                                                                          text=part.chunk.text),
                                         MAP_TOKENS, model_override).text.strip()
            progress("map", len(parts), len(parts))
            # 要点太多放不下：相邻部分合并一轮（位置随之合并），直到能放下
            while sum(estimate_tokens(p.points) + 12 for p in parts) > final_budget and len(parts) > 1:
                if cancelled():
                    raise Cancelled("已停止总结")
                groups = _pack(parts, lambda p: estimate_tokens(p.points) + 12, max(final_budget // 2, 400))
                if len(groups) == len(parts):   # 每个部分本身就超长：截短要点
                    for p in parts:
                        p.points = p.points[: max(80, final_budget // len(parts))]
                    break
                merged = []
                for g in groups:
                    chunk = reduce(merge_pair, [p.chunk for p in g])
                    joined = "\n".join(p.points for p in g)
                    points = self._chat(P["system_map"], P["map"].format(name=name, loc="", text=joined),
                                        MAP_TOKENS, model_override).text.strip() if len(g) > 1 else joined
                    merged.append(Part(chunk, points))
                parts = merged
            label = P["label_points"]
            material = [p.points for p in parts]

        context = [replace(p.chunk, score=0.0) for p in parts]
        if on_context and label == P["label_text"]:
            on_context(context)
        sources = "\n\n".join(
            f"[{i}]（{p.chunk.location.describe(p.chunk.file_type) or name}）\n{m}"
            for i, (p, m) in enumerate(zip(parts, material), start=1))
        if cancelled():
            raise Cancelled("已停止总结")
        progress("final", 0, 1)
        result = self._chat(P["system_final"], P["final"].format(name=name, label=label, sources=sources),
                            FINAL_TOKENS, model_override, on_token=on_token)
        text, citations, invalid = validate_citations(result.text, context)
        return Answer(text=text, citations=citations, invalid_refs=invalid, no_context=False, context=context,
                      ttft_ms=result.ttft_ms, latency_ms=result.latency_ms,
                      input_tokens=result.input_tokens, output_tokens=result.output_tokens)
