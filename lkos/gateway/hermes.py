"""Hermes Proxy 远端 Provider：POC 只保留接口，任何调用都明确失败。

接入时需要确定：是否兼容 OpenAI API、认证方式（密钥从 macOS 钥匙串读取）、超时与重试策略，
以及发送前的用户确认流程（需求 FR-PR4）。
"""

from __future__ import annotations

from lkos.errors import ModelUnavailableError
from lkos.gateway.base import ChatOptions, ChatResult, ModelCheck, TokenCallback


class HermesProvider:
    is_remote = True

    def __init__(self, name: str, base_url: str | None = None, **_: object):
        self.name = name
        self.base_url = base_url

    def chat(self, model: str, messages: list[dict], opts: ChatOptions,
             on_token: TokenCallback | None = None) -> ChatResult:
        raise ModelUnavailableError("Hermes Proxy 在 POC 中尚未实现。")

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        raise ModelUnavailableError("远端 Embedding 不在设计范围内。")

    def check_model(self, model: str) -> ModelCheck:
        return ModelCheck(False, "Hermes Proxy 在 POC 中尚未实现")
