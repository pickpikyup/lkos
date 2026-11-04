"""Ollama Provider：/api/chat（流式）、/api/embed、/api/tags。"""

from __future__ import annotations

import json
import time

import httpx

from lkos.errors import ModelUnavailableError
from lkos.gateway.base import ChatOptions, ChatResult, ModelCheck, TokenCallback


class OllamaProvider:
    is_remote = False

    def __init__(self, name: str, base_url: str = "http://127.0.0.1:11434", keep_alive: str = "30m",
                 timeout_s: float = 120, transport: httpx.BaseTransport | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.keep_alive = keep_alive
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            transport=transport,
            trust_env=False,  # 本机服务不走系统代理
        )

    # ---------- 错误映射 ----------
    def _wrap(self, model: str, exc: Exception) -> ModelUnavailableError:
        if isinstance(exc, httpx.ConnectError):
            return ModelUnavailableError(f"无法连接 Ollama（{self.base_url}）。请先启动 Ollama 应用或执行 `ollama serve`。")
        if isinstance(exc, httpx.TimeoutException):
            return ModelUnavailableError(f"调用 {model} 超时。可能是内存不足或模型首次加载较慢。")
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            body = exc.response.text[:300]
            if status == 404:
                return ModelUnavailableError(f"Ollama 中没有模型 {model}。请执行：ollama pull {model}")
            return ModelUnavailableError(f"Ollama 返回 HTTP {status}：{body}")
        return ModelUnavailableError(f"调用 Ollama 失败：{type(exc).__name__}: {exc}")

    # ---------- chat ----------
    def chat(self, model: str, messages: list[dict], opts: ChatOptions,
             on_token: TokenCallback | None = None) -> ChatResult:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": opts.think,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": opts.temperature,
                "num_ctx": opts.num_ctx,  # 必须显式传入，Ollama 默认上下文更短
                "num_predict": opts.max_tokens,
            },
        }
        try:
            return self._stream_chat(model, payload, on_token)
        except ModelUnavailableError as e:
            # 不支持思考开关的模型（如纯 Instruct 版本）会拒绝 think 参数：去掉后重试一次
            if "think" in payload and "think" in str(e).lower():
                payload.pop("think")
                return self._stream_chat(model, payload, on_token)
            raise

    def _stream_chat(self, model: str, payload: dict, on_token: TokenCallback | None) -> ChatResult:
        start = time.perf_counter()
        ttft: int | None = None
        parts: list[str] = []
        final: dict = {}
        try:
            with self.client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise ModelUnavailableError(f"Ollama 报错：{data['error']}")
                    piece = (data.get("message") or {}).get("content") or ""
                    if piece:
                        if ttft is None:
                            ttft = int((time.perf_counter() - start) * 1000)
                        parts.append(piece)
                        if on_token:
                            on_token(piece)
                    if data.get("done"):
                        final = data
                        break
        except ModelUnavailableError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            raise self._wrap(model, e) from e
        load_ns = final.get("load_duration")
        return ChatResult(
            text="".join(parts),
            input_tokens=final.get("prompt_eval_count"),
            output_tokens=final.get("eval_count"),
            ttft_ms=ttft,
            latency_ms=int((time.perf_counter() - start) * 1000),
            load_ms=int(load_ns / 1e6) if isinstance(load_ns, (int, float)) else None,
        )

    # ---------- embed ----------
    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = self.client.post("/api/embed", json={"model": model, "input": texts, "keep_alive": self.keep_alive})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            raise self._wrap(model, e) from e
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ModelUnavailableError(f"Ollama /api/embed 返回格式异常：{str(data)[:200]}")
        return vectors

    # ---------- 可用性 ----------
    def list_models(self) -> list[str]:
        try:
            resp = self.client.get("/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise self._wrap("-", e) from e
        return [m.get("name", "") for m in resp.json().get("models", [])]

    def check_model(self, model: str) -> ModelCheck:
        try:
            names = self.list_models()
        except ModelUnavailableError as e:
            return ModelCheck(False, str(e))
        wanted = model if ":" in model else model + ":latest"
        if model in names or wanted in names:
            return ModelCheck(True, "已下载")
        return ModelCheck(False, f"未下载，请执行：ollama pull {model}")
