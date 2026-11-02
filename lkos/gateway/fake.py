"""确定性的假 Provider，用于单元测试、功能测试，以及没有 Ollama 时的演示。

- embed：词袋哈希向量（L2 归一化），共享词越多余弦越近，足以验证检索流程；
- chat：从提示中找到第一个资料片段，复述它并引用 [1]；可通过选项模拟无效引用或故障。
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Callable

from lkos.errors import ModelUnavailableError
from lkos.gateway.base import ChatOptions, ChatResult, ModelCheck, TokenCallback
from lkos.indexing.tokenize import query_terms

_SNIPPET = re.compile(r"^\[(\d+)\][^\n]*\n(.+?)(?=\n\[\d+\]|\n\n|\Z)", re.S | re.M)


class FakeProvider:
    is_remote = False

    def __init__(self, name: str, dim: int = 64, invalid_ref: bool = False, fail: bool = False,
                 answer_fn: Callable[[list[dict]], str] | None = None, **_: object):
        self.name = name
        self.dim = int(dim)
        self.invalid_ref = bool(invalid_ref)
        self.fail = bool(fail)
        self.answer_fn = answer_fn
        self.chat_calls: list[dict] = []
        self.embed_calls: list[list[str]] = []

    # ---------- embed ----------
    def _vector(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for term in query_terms(text) or [text.lower()]:
            h = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            v[idx] += sign
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v] if any(v) else [1.0 / math.sqrt(self.dim)] * self.dim

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise ModelUnavailableError("FakeProvider 被配置为故障")
        self.embed_calls.append(list(texts))
        return [self._vector(t) for t in texts]

    # ---------- chat ----------
    def chat(self, model: str, messages: list[dict], opts: ChatOptions,
             on_token: TokenCallback | None = None) -> ChatResult:
        if self.fail:
            raise ModelUnavailableError("FakeProvider 被配置为故障")
        start = time.perf_counter()
        self.chat_calls.append({"model": model, "messages": messages, "opts": opts})
        if self.answer_fn:
            text = self.answer_fn(messages)
        else:
            user = messages[-1]["content"]
            m = _SNIPPET.search(user)
            if m:
                text = f"根据资料：{m.group(2).strip()[:80]} [{m.group(1)}]"
            else:
                text = "资料中没有找到相关信息。"
            if self.invalid_ref:
                text += " 另见 [99]。"
        for piece in re.findall(r".{1,8}", text, re.S):
            if on_token:
                on_token(piece)
        prompt_chars = sum(len(m["content"]) for m in messages)
        return ChatResult(text=text, input_tokens=prompt_chars // 2, output_tokens=len(text) // 2,
                          ttft_ms=1, latency_ms=int((time.perf_counter() - start) * 1000))

    def check_model(self, model: str) -> ModelCheck:
        return ModelCheck(not self.fail, "FakeProvider" if not self.fail else "FakeProvider 被配置为故障")
