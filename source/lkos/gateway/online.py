"""在线模型 Provider：Anthropic（Claude Messages API）与 OpenAI 兼容接口（/chat/completions）。

都用流式输出（SSE），返回首字时间、token 数；网络走系统代理（trust_env=True）。
错误都转成 ModelUnavailableError，写明原因（密钥无效、模型不存在、额度不足、网络不通……）。
"""

from __future__ import annotations

import json
import time

import httpx

from lkos.errors import Cancelled, ModelUnavailableError
from lkos.gateway.base import ChatOptions, ChatResult, ModelCheck, TokenCallback

ANTHROPIC_VERSION = "2023-06-01"


class _Online:
    is_remote = True
    label = "在线模型"

    def __init__(self, name: str, base_url: str, api_key: str, timeout_s: float = 120,
                 transport: httpx.BaseTransport | None = None):
        self.name = name
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0), transport=transport,
                                   trust_env=True, follow_redirects=True)

    def _wrap(self, model: str, exc: Exception) -> ModelUnavailableError:
        if isinstance(exc, ModelUnavailableError):
            return exc
        if isinstance(exc, httpx.ConnectError):
            return ModelUnavailableError(f"连不上 {self.base_url}：请检查网络、代理或 API 地址。")
        if isinstance(exc, httpx.TimeoutException):
            return ModelUnavailableError(f"{self.label} {model} 响应超时，请稍后再试。")
        if isinstance(exc, httpx.HTTPStatusError):
            status, body = exc.response.status_code, exc.response.text[:300]
            msg = _error_message(body)
            if status in (401, 403):
                return ModelUnavailableError(f"API 密钥无效或没有权限（HTTP {status}）：{msg}")
            if status == 404:
                return ModelUnavailableError(f"找不到模型 {model} 或接口地址不对（HTTP 404）：{msg}")
            if status == 429:
                return ModelUnavailableError(f"请求太频繁或额度不足（HTTP 429）：{msg}")
            if status == 400:
                return ModelUnavailableError(f"请求被拒绝（HTTP 400）：{msg}")
            return ModelUnavailableError(f"{self.label}返回 HTTP {status}：{msg}")
        return ModelUnavailableError(f"调用{self.label}失败：{type(exc).__name__}: {exc}")

    def _need_key(self) -> None:
        if not self.base_url:
            raise ModelUnavailableError("没有填写 API 地址")
        if not self.api_key:
            raise ModelUnavailableError("没有填写 API 密钥")

    def _sse(self, url: str, headers: dict, payload: dict, model: str, handle, on_token) -> ChatResult:
        self._need_key()
        start = time.perf_counter()
        state = {"parts": [], "ttft": None, "in": None, "out": None}

        def emit(text: str) -> None:
            if not text:
                return
            if state["ttft"] is None:
                state["ttft"] = int((time.perf_counter() - start) * 1000)
            state["parts"].append(text)
            if on_token:
                on_token(text)

        try:
            with self.client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    resp.raise_for_status()
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        handle(json.loads(data), emit, state)
                    except ValueError:
                        continue
        except (ModelUnavailableError, Cancelled):
            raise
        except Exception as e:  # noqa: BLE001 - 网络与 HTTP 异常
            raise self._wrap(model, e) from e
        return ChatResult(text="".join(state["parts"]), input_tokens=state["in"], output_tokens=state["out"],
                          ttft_ms=state["ttft"], latency_ms=int((time.perf_counter() - start) * 1000))

    def check_model(self, model: str) -> ModelCheck:
        try:
            r = self.chat(model, [{"role": "user", "content": "回复“好”"}], ChatOptions(max_tokens=8, temperature=0))
        except ModelUnavailableError as e:
            return ModelCheck(False, str(e))
        return ModelCheck(True, f"连接正常（{r.latency_ms} ms）")

    def embed(self, model: str, texts: list[str]):  # 向量只用本机模型
        raise ModelUnavailableError("在线模型不用于向量计算")


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:200]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:200]
    return str(err or data.get("message") or body)[:200]


class AnthropicProvider(_Online):
    label = "Claude"

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}

    def _url(self, path: str) -> str:
        base = self.base_url
        return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"

    def chat(self, model: str, messages: list[dict], opts: ChatOptions,
             on_token: TokenCallback | None = None) -> ChatResult:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        msgs = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
        payload = {"model": model, "max_tokens": opts.max_tokens, "temperature": opts.temperature,
                   "messages": msgs, "stream": True}
        if system:
            payload["system"] = system

        def handle(ev: dict, emit, state) -> None:
            t = ev.get("type")
            if t == "content_block_delta":
                emit((ev.get("delta") or {}).get("text") or "")
            elif t == "message_start":
                state["in"] = ((ev.get("message") or {}).get("usage") or {}).get("input_tokens")
            elif t == "message_delta":
                state["out"] = (ev.get("usage") or {}).get("output_tokens", state["out"])
            elif t == "error":
                raise ModelUnavailableError(f"Claude 返回错误：{(ev.get('error') or {}).get('message') or ev}")

        return self._sse(self._url("/messages"), self._headers(), payload, model, handle, on_token)

    def list_models(self) -> list[str]:
        self._need_key()
        try:
            r = self.client.get(self._url("/models"), headers=self._headers(), params={"limit": 100})
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise self._wrap("", e) from e
        return [m["id"] for m in r.json().get("data", []) if m.get("id")]


class OpenAICompatProvider(_Online):
    label = "在线模型"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "content-type": "application/json"}

    def chat(self, model: str, messages: list[dict], opts: ChatOptions,
             on_token: TokenCallback | None = None) -> ChatResult:
        payload = {"model": model, "messages": messages, "stream": True, "temperature": opts.temperature,
                   "max_tokens": opts.max_tokens}

        def handle(ev: dict, emit, state) -> None:
            if ev.get("error"):
                raise ModelUnavailableError(f"在线模型返回错误：{_error_message(json.dumps(ev))}")
            for ch in ev.get("choices") or []:
                emit((ch.get("delta") or {}).get("content") or "")
            usage = ev.get("usage") or {}
            if usage:
                state["in"] = usage.get("prompt_tokens", state["in"])
                state["out"] = usage.get("completion_tokens", state["out"])

        return self._sse(f"{self.base_url}/chat/completions", self._headers(), payload, model, handle, on_token)

    def list_models(self) -> list[str]:
        self._need_key()
        try:
            r = self.client.get(f"{self.base_url}/models", headers=self._headers())
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise self._wrap("", e) from e
        return sorted(m["id"] for m in r.json().get("data", []) if m.get("id"))
