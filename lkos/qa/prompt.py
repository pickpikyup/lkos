"""提示模板与上下文预算（设计文档第 10 节）。"""

from __future__ import annotations

import math
import re
from typing import Sequence

from lkos.types import RetrievedChunk

_CJK = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")

SYSTEM_PROMPTS = {
    "zh": (
        "你是知识库助手。只根据下面编号的资料回答。\n"
        "每个事实陈述后用 [编号] 标注出处，只能使用列出的编号。\n"
        "资料中没有答案时，直接说明“资料中没有找到相关信息”，不要猜测。\n"
        "用中文回答，简洁准确。"
    ),
    "en": (
        "You are a knowledge-base assistant. Answer only from the numbered sources below.\n"
        "After every factual statement, cite its source as [n], using only the listed numbers.\n"
        "If the sources do not contain the answer, say \"No relevant information was found in the sources.\" Do not guess.\n"
        "Answer in English, concisely."
    ),
    "fr": (
        "Vous êtes un assistant de base de connaissances. Répondez uniquement à partir des sources numérotées ci-dessous.\n"
        "Après chaque affirmation, citez sa source sous la forme [n], en n'utilisant que les numéros listés.\n"
        "Si les sources ne contiennent pas la réponse, dites « Aucune information pertinente n'a été trouvée dans les sources. » Ne devinez pas.\n"
        "Répondez en français, de façon concise."
    ),
}

LABELS = {
    "zh": ("资料：", "问题："),
    "en": ("Sources:", "Question:"),
    "fr": ("Sources :", "Question :"),
}

NO_CONTEXT_MESSAGE = {
    "zh": "知识库中没有找到与问题相关的资料。",
    "en": "No relevant documents were found in the knowledge base.",
    "fr": "Aucun document pertinent n'a été trouvé dans la base de connaissances.",
}

OUTPUT_MARGIN = 300


def lang_key(lang: str) -> str:
    return lang if lang in SYSTEM_PROMPTS else "en"


def estimate_tokens(text: str) -> int:
    """保守估算：汉字（含全角标点）每个按 1 个 token，其他字符每 3 个按 1 个 token。"""
    cjk = len(_CJK.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 3)


def format_source(n: int, chunk: RetrievedChunk) -> str:
    return f"[{n}] ({chunk.describe()})\n{chunk.text}"


def fit_context(question: str, hits: Sequence[RetrievedChunk], lang: str, num_ctx: int,
                max_output: int) -> list[RetrievedChunk]:
    """按分数从高到低装入片段，直到 token 预算用完。"""
    key = lang_key(lang)
    budget = (num_ctx - max_output - OUTPUT_MARGIN
              - estimate_tokens(SYSTEM_PROMPTS[key]) - estimate_tokens(question) - 20)
    out: list[RetrievedChunk] = []
    for i, hit in enumerate(hits, start=1):
        cost = estimate_tokens(format_source(i, hit)) + 2
        if cost > budget:
            break
        out.append(hit)
        budget -= cost
    return out


def build_messages(question: str, context: Sequence[RetrievedChunk], lang: str) -> list[dict]:
    key = lang_key(lang)
    src_label, q_label = LABELS[key]
    sources = "\n\n".join(format_source(i, c) for i, c in enumerate(context, start=1))
    return [
        {"role": "system", "content": SYSTEM_PROMPTS[key]},
        {"role": "user", "content": f"{src_label}\n{sources}\n\n{q_label}{question}"},
    ]
