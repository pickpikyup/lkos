"""对话上下文：把同一个对话里之前的轮次带给模型。

瓶颈在上下文长度，所以本机模型与在线模型用不同的策略：
- 本机模型（4K 上下文）：只带最近 LOCAL_TURNS 轮，每个回答截到 LOCAL_ANSWER_CHARS 字，资料只用本轮检索到的；
- 在线模型（几万到几十万 token）：带上整个对话（太长时从最早的轮次开始丢），并把之前各轮找到的资料片段
  一起送去——本机检索到的信息，换成在线模型追问时也能用上。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from lkos.qa.prompt import estimate_tokens
from lkos.indexing.language import detect_lang
from lkos.types import Location, RetrievedChunk

LOCAL_TURNS = 3
LOCAL_ANSWER_CHARS = 400
ONLINE_ANSWER_CHARS = 6000
ONLINE_HISTORY_SHARE = 0.35      # 在线模型：对话历史最多占上下文的这么多
_REF = re.compile(r"\s*\[\d+(?:\s*[,，、]\s*\d+)*\]")


@dataclass
class Turn:
    question: str
    answer: str
    kind: str = "qa"
    context: list[RetrievedChunk] = field(default_factory=list)


@dataclass(frozen=True)
class _FixedLocation(Location):
    """从问答记录里恢复的位置：直接用当时显示的文字。"""
    text: str = ""

    def describe(self, file_type: str = "") -> str:  # noqa: ARG002
        return self.text


def chunk_from_dict(d: dict) -> RetrievedChunk:
    text = d.get("text") or ""
    cid = d.get("id") or -int(hashlib.md5(f"{d.get('path')}\n{text}".encode()).hexdigest()[:12], 16)
    return RetrievedChunk(chunk_id=int(cid), file_path=d.get("path") or d.get("file") or "", file_type=d.get("type") or "",
                          text=text, lang=d.get("lang") or "", location=_FixedLocation(page=d.get("page"), text=d.get("loc") or ""),
                          score=float(d.get("score") or 0.0))


def turns_from_records(rows: list[dict]) -> list[Turn]:
    """问答记录 → 对话轮次（关键词检索不调用模型，不算进对话上下文）。"""
    import json

    out = []
    for r in rows:
        if (r.get("kind") or "qa") == "search":
            continue
        try:
            ctx = [chunk_from_dict(d) for d in json.loads(r.get("context_json") or "[]")]
        except ValueError:
            ctx = []
        q = re.sub(r"^(分析|提取)：", "", r["question"])
        out.append(Turn(question=q, answer=r["answer"], kind=r.get("kind") or "qa", context=ctx))
    return out


def clean_answer(text: str, limit: int) -> str:
    """去掉 [n]（编号对应的是那一轮的资料，不是本轮的）并截短。"""
    a = _REF.sub("", text or "").strip()
    return a if len(a) <= limit else a[:limit].rstrip() + "……"


CHAT_HISTORY_SHARE = 0.6        # 纯对话不带资料：对话历史可以占更多上下文
CHAT_ANSWER_CHARS = 1500


def history_messages(turns: list[Turn], online: bool, num_ctx: int, chat: bool = False) -> list[dict]:
    """chat=True（纯对话，不查资料）：本机模型也按预算尽量多带几轮。"""
    if not turns:
        return []
    if chat and not online:
        return _fit_turns(turns, int(num_ctx * CHAT_HISTORY_SHARE), CHAT_ANSWER_CHARS)
    if not online:
        msgs = []
        for t in turns[-LOCAL_TURNS:]:
            msgs += [{"role": "user", "content": t.question},
                     {"role": "assistant", "content": clean_answer(t.answer, LOCAL_ANSWER_CHARS)}]
        return msgs
    return _fit_turns(turns, int(num_ctx * (CHAT_HISTORY_SHARE if chat else ONLINE_HISTORY_SHARE)), ONLINE_ANSWER_CHARS)


def _fit_turns(turns: list[Turn], budget: int, answer_chars: int) -> list[dict]:
    """从最近的一轮往前装，直到预算用完。"""
    picked: list[list[dict]] = []
    for t in reversed(turns):
        pair = [{"role": "user", "content": t.question},
                {"role": "assistant", "content": clean_answer(t.answer, answer_chars)}]
        cost = sum(estimate_tokens(m["content"]) + 4 for m in pair)
        if cost > budget:
            break
        picked.append(pair)
        budget -= cost
    return [m for pair in reversed(picked) for m in pair]


def messages_tokens(msgs: list[dict]) -> int:
    return sum(estimate_tokens(m["content"]) + 4 for m in msgs)


def prior_chunks(turns: list[Turn], exclude: set) -> list[RetrievedChunk]:
    """之前各轮的资料片段（最近的轮次在前，去重），给在线模型补充上下文。"""
    out, seen = [], set(exclude)
    for t in reversed(turns):
        for c in t.context:
            key = (c.file_path, c.text[:200])
            if c.chunk_id in seen or key in seen:
                continue
            seen.update({c.chunk_id, key})
            out.append(c)
    return out


def conversation_lang(questions: list[str]) -> str:
    """深度分析的回答语言：默认中文；这个对话里用户一直用英文或法文提问时，用那种语言。"""
    langs = []
    for q in questions:
        if re.search(r"[㐀-鿿]", q):
            langs.append("zh")
        elif len(re.findall(r"[A-Za-zÀ-ÿ]{2,}", q)) >= 3:
            langs.append(detect_lang(q))
    if not langs:
        return "zh"
    recent = langs[-3:]
    for lang in ("en", "fr"):
        if recent.count(lang) > len(recent) / 2:
            return lang
    return "zh"
