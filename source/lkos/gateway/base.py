"""Provider 协议：上层只依赖这里的接口，不直接调用具体模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

TokenCallback = Callable[[str], None]
ThinkingCallback = Callable[[str], None]   # 推理模型的思考过程（不进回答正文）


@dataclass
class ChatOptions:
    temperature: float = 0.3
    max_tokens: int = 1024
    num_ctx: int = 8192
    think: bool = False


@dataclass
class ChatResult:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    ttft_ms: int | None
    latency_ms: int
    load_ms: int | None = None  # 模型加载耗时（冷启动时很大），Ollama 返回 load_duration
    thinking: str = ""          # 推理模型的思考过程（DeepSeek 的 reasoning_content、Claude 的 thinking 等）
    finish_reason: str | None = None   # 结束原因，统一为 stop / length（输出额度用完）/ 其他原始值

    @property
    def out_of_budget(self) -> bool:
        """输出额度用完、却没写出正文（额度都花在思考上了）。"""
        return not self.text.strip() and self.finish_reason == "length"


@dataclass
class ModelCheck:
    ok: bool
    message: str


class ChatProvider(Protocol):
    name: str
    is_remote: bool

    def chat(self, model: str, messages: list[dict], opts: ChatOptions,
             on_token: TokenCallback | None = None,
             on_thinking: ThinkingCallback | None = None) -> ChatResult: ...

    def check_model(self, model: str) -> ModelCheck: ...


class EmbeddingProvider(Protocol):
    name: str

    def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...

    def check_model(self, model: str) -> ModelCheck: ...
