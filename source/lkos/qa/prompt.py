"""提示模板与上下文预算（设计文档第 10 节）。"""

from __future__ import annotations

import math
import re
from typing import Sequence

from lkos.types import RetrievedChunk

_CJK = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")

SYSTEM_PROMPTS = {
    "zh": (
        "你是知识库助手。只根据下面编号的资料回答，不使用资料以外的知识。\n"
        "每个事实陈述后用 [编号] 标注出处，只能使用列出的编号。\n"
        "问题有多个部分时，逐个回答：资料能回答的部分直接回答并标注出处；资料没有提到的部分，明确说“资料中没有提到……”。\n"
        "“是什么”“有哪些”“为什么”这类问题，可以把资料里的相关内容归纳整理后回答。\n"
        "只有当所有资料都与问题无关时，才回答“资料中没有找到相关信息”。不要猜测，不要编造数字。\n"
        "用中文回答，简洁准确。"
    ),
    "en": (
        "You are a knowledge-base assistant. Answer only from the numbered sources below; do not use outside knowledge.\n"
        "After every factual statement, cite its source as [n], using only the listed numbers.\n"
        "If the question has several parts, answer each one: answer what the sources support with citations, and say "
        "\"The sources do not mention ...\" for the parts they do not cover.\n"
        "For \"what is\", \"which\" or \"why\" questions you may summarise the relevant content of the sources.\n"
        "Only if none of the sources is relevant, say \"No relevant information was found in the sources.\" "
        "Do not guess or invent numbers.\n"
        "Answer in English, concisely."
    ),
    "fr": (
        "Vous êtes un assistant de base de connaissances. Répondez uniquement à partir des sources numérotées ci-dessous.\n"
        "Après chaque affirmation, citez sa source sous la forme [n], en n'utilisant que les numéros listés.\n"
        "Si la question comporte plusieurs parties, répondez à chacune : ce que les sources permettent, avec citations ; "
        "pour le reste, dites « Les sources ne mentionnent pas … ».\n"
        "Pour les questions « qu'est-ce que », « lesquels » ou « pourquoi », vous pouvez résumer le contenu pertinent des sources.\n"
        "Seulement si aucune source n'est pertinente, dites « Aucune information pertinente n'a été trouvée dans les sources. » "
        "Ne devinez pas, n'inventez pas de chiffres.\n"
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


HISTORY_ANSWER_CHARS = 400   # 追问时带上的上一轮回答最多这么长（去掉出处编号）


def history_turns(history) -> list[dict]:
    """上一轮问答 → 对话消息（只保留最近 2 轮；回答去掉 [n]，因为编号对应的是上一轮的资料）。"""
    out: list[dict] = []
    for q, a in (history or [])[-2:]:
        a = re.sub(r"\s*\[\d+(?:\s*[,，、]\s*\d+)*\]", "", a or "").strip()[:HISTORY_ANSWER_CHARS]
        out += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    return out


def history_tokens(history) -> int:
    return sum(estimate_tokens(m["content"]) + 4 for m in history_turns(history))


def build_messages(question: str, context: Sequence[RetrievedChunk], lang: str, history=None) -> list[dict]:
    key = lang_key(lang)
    src_label, q_label = LABELS[key]
    sources = "\n\n".join(format_source(i, c) for i, c in enumerate(context, start=1))
    return [
        {"role": "system", "content": SYSTEM_PROMPTS[key]},
        *history_turns(history),
        {"role": "user", "content": f"{src_label}\n{sources}\n\n{q_label}{question}"},
    ]
