"""在线模型 Provider：Anthropic（Claude Messages API）与 OpenAI 兼容接口（/chat/completions）。

都用流式输出（SSE），返回首字时间、token 数、思考过程、结束原因；网络走系统代理（trust_env=True）。
错误都转成 ModelUnavailableError，写明原因（密钥无效、模型不存在、额度不足、网络不通……）。

参数（来自 models.json，见 models_config.ADVANCED_KEYS）：
- 输出上限：ChatOptions.max_tokens 为 None 时，OpenAI 兼容接口不传（按服务商默认），Claude 接口用 16000；
- temperature：没配置时用任务默认值；配置为 null 时不传；
- max_tokens_param：max_tokens / max_completion_tokens；
- extra_body / extra_body_short（只用于简短的内部调用）/ headers：原样合并进请求。

参数不兼容时自动调整后重试（最多 3 次），并记住调整，之后的请求直接用调整后的参数：
- 提示 temperature 不支持 → 不再传 temperature；
- 提示 max_tokens 超出上限（消息里带有更小的数字）→ 改用那个数字；
- 提示 max_tokens 不支持、要用 max_completion_tokens → 换参数名；其他 max_tokens 问题（OpenAI 兼容）→ 不再传；
- 简短调用带了 extra_body_short 被拒绝 → 简短调用不再加它。
"""

from __future__ import annotations

import copy
import json
import re
import time

import httpx

from lkos.errors import Cancelled, ModelUnavailableError
from lkos.gateway.base import ChatOptions, ChatResult, ModelCheck, TokenCallback
from lkos.logs import get_logger

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_DEFAULT_MAX = 16000
_UNSET = object()
log = get_logger("model")


class _BadRequest(ModelUnavailableError):
    """HTTP 400 / 422：请求参数被拒绝（可能可以调整参数后重试）。"""

    def __init__(self, message: str, detail: str):
        super().__init__(message)
        self.detail = detail


class _Online:
    is_remote = True
    label = "在线模型"

    def __init__(self, name: str, base_url: str, api_key: str, timeout_s: float = 120,
                 transport: httpx.BaseTransport | None = None, temperature=_UNSET, max_tokens_param: str | None = None,
                 extra_body: dict | None = None, extra_body_short: dict | None = None, headers: dict | None = None,
                 **_ignored):
        self.name = name
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.temperature = temperature            # _UNSET：用任务默认值；None：不传
        self.max_tokens_param = max_tokens_param or "max_tokens"
        self.extra_body = dict(extra_body or {})
        self.extra_body_short = dict(extra_body_short or {})
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self._max_cap: int | None = None          # 服务端告诉我们的输出上限
        self._omit_max = False
        self._no_short = False
        self.client = httpx.Client(timeout=httpx.Timeout(float(timeout_s or 120), connect=10.0), transport=transport,
                                   trust_env=True, follow_redirects=True)

    # ---------- 错误 ----------
    def _wrap(self, model: str, exc: Exception) -> ModelUnavailableError:
        if isinstance(exc, ModelUnavailableError):
            return exc
        if isinstance(exc, httpx.ConnectError):
            return ModelUnavailableError(f"连不上 {self.base_url}：请检查网络、代理或 API 地址。")
        if isinstance(exc, httpx.TimeoutException):
            return ModelUnavailableError(f"{self.label} {model} 响应超时，请稍后再试。")
        if isinstance(exc, httpx.HTTPStatusError):
            status, body = exc.response.status_code, exc.response.text[:600]
            msg = _error_message(body)
            if status in (401, 403):
                return ModelUnavailableError(f"API 密钥无效或没有权限（HTTP {status}）：{msg}")
            if status == 404:
                return ModelUnavailableError(f"找不到模型 {model} 或接口地址不对（HTTP 404）：{msg}")
            if status == 429:
                return ModelUnavailableError(f"请求太频繁或额度不足（HTTP 429）：{msg}")
            if status in (400, 422):
                return _BadRequest(f"请求被拒绝（HTTP {status}）：{msg}", msg)
            return ModelUnavailableError(f"{self.label}返回 HTTP {status}：{msg}")
        return ModelUnavailableError(f"调用{self.label}失败：{type(exc).__name__}: {exc}")

    def _need_key(self) -> None:
        if not self.base_url:
            raise ModelUnavailableError("没有填写 API 地址")
        if not self.api_key:
            raise ModelUnavailableError("没有填写 API 密钥")

    # ---------- 请求参数 ----------
    def _common(self, payload: dict, opts: ChatOptions, required_max: int | None = None) -> dict:
        temp = opts.temperature if self.temperature is _UNSET else self.temperature
        if temp is not None:
            payload["temperature"] = temp
        limit = opts.max_tokens or required_max
        if limit and self._max_cap:
            limit = min(limit, self._max_cap)
        if limit and not (self._omit_max and required_max is None):
            payload[self.max_tokens_param] = limit
        payload.update(copy.deepcopy(self.extra_body))
        if opts.short and not self._no_short:
            payload.update(copy.deepcopy(self.extra_body_short))
        return payload

    def _adapt(self, payload: dict, detail: str, short: bool, can_omit_max: bool) -> str | None:
        """根据 400 的错误信息调整参数；返回调整说明，没法调整时返回 None。"""
        low = detail.lower()
        param = next((p for p in ("max_tokens", "max_completion_tokens") if p in payload), None)
        if "temperature" in low and "temperature" in payload:
            payload.pop("temperature")
            self.temperature = None
            return "不传 temperature"
        if param and ("max_tokens" in low or "max_completion_tokens" in low or "max output" in low):
            cur = int(payload[param])
            smaller = [int(n) for n in re.findall(r"(?<![\d.])(\d{3,7})(?![\d.])", detail) if 16 <= int(n) < cur]
            if param == "max_tokens" and "max_completion_tokens" in low and not smaller:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                self.max_tokens_param = "max_completion_tokens"
                return "改用 max_completion_tokens"
            if smaller:
                payload[param] = self._max_cap = max(smaller)
                return f"输出上限改为 {self._max_cap}"
            if can_omit_max:
                payload.pop(param)
                self._omit_max = True
                return f"不传 {param}（按服务商默认）"
        if short and self.extra_body_short and not self._no_short:
            for k in self.extra_body_short:
                payload.pop(k, None)
            self._no_short = True
            return "简短调用不再附加 extra_body_short"
        return None

    def _send(self, url: str, headers: dict, payload: dict, model: str, handle, on_token, on_thinking, short: bool,
              can_omit_max: bool) -> ChatResult:
        for attempt in range(4):
            try:
                return self._sse(url, headers, payload, model, handle, on_token, on_thinking)
            except _BadRequest as e:
                fix = self._adapt(payload, e.detail, short, can_omit_max) if attempt < 3 else None
                if not fix:
                    raise
                log.warning("%s %s 拒绝了请求参数（%s）：%s 后重试", self.label, model, e.detail[:120], fix)
        raise ModelUnavailableError("请求参数多次被拒绝")  # pragma: no cover - 循环里已经返回或抛出

    def _sse(self, url: str, headers: dict, payload: dict, model: str, handle, on_token, on_thinking=None) -> ChatResult:
        self._need_key()
        start = time.perf_counter()
        state = {"parts": [], "thinking": [], "ttft": None, "in": None, "out": None, "finish": None}

        def emit(text: str) -> None:
            if not text:
                return
            if state["ttft"] is None:
                state["ttft"] = int((time.perf_counter() - start) * 1000)
            state["parts"].append(text)
            if on_token:
                on_token(text)

        def think(text: str) -> None:
            """推理模型的思考过程：单独收集、单独推给界面，不混进回答正文。"""
            if not text:
                return
            state["thinking"].append(text)
            if on_thinking:
                on_thinking(text)

        emit.think = think
        try:
            with self.client.stream("POST", url, headers={**headers, **self.headers}, json=payload) as resp:
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
                          ttft_ms=state["ttft"], latency_ms=int((time.perf_counter() - start) * 1000),
                          thinking="".join(state["thinking"]), finish_reason=state["finish"])

    def check_model(self, model: str) -> ModelCheck:
        try:
            r = self.chat(model, [{"role": "user", "content": "回复“好”"}], ChatOptions(max_tokens=16, temperature=0,
                                                                                    short=True))
        except ModelUnavailableError as e:
            return ModelCheck(False, str(e))
        return ModelCheck(True, f"连接正常（{r.latency_ms} ms）")

    def embed(self, model: str, texts: list[str]):  # 向量只用本机模型
        raise ModelUnavailableError("在线模型不用于向量计算")


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:300]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    if isinstance(data, dict) and isinstance(data.get("detail"), (str, list)):
        return str(data["detail"])[:300]
    return str(err or (data.get("message") if isinstance(data, dict) else data) or body)[:300]


class AnthropicProvider(_Online):
    label = "Claude"

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}

    def _url(self, path: str) -> str:
        base = self.base_url
        return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"

    def chat(self, model: str, messages: list[dict], opts: ChatOptions,
             on_token: TokenCallback | None = None, on_thinking=None) -> ChatResult:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        msgs = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
        payload = {"model": model, "messages": msgs, "stream": True}
        if system:
            payload["system"] = system
        self.max_tokens_param = "max_tokens"           # Claude 接口只认 max_tokens，而且必须给
        payload = self._common(payload, opts, required_max=ANTHROPIC_DEFAULT_MAX)

        def handle(ev: dict, emit, state) -> None:
            t = ev.get("type")
            if t == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "thinking_delta":
                    emit.think(delta.get("thinking") or "")
                else:
                    emit(delta.get("text") or "")
            elif t == "message_start":
                state["in"] = ((ev.get("message") or {}).get("usage") or {}).get("input_tokens")
            elif t == "message_delta":
                state["out"] = (ev.get("usage") or {}).get("output_tokens", state["out"])
                stop = (ev.get("delta") or {}).get("stop_reason")
                if stop:
                    state["finish"] = "length" if stop == "max_tokens" else ("stop" if stop == "end_turn" else stop)
            elif t == "error":
                raise ModelUnavailableError(f"Claude 返回错误：{(ev.get('error') or {}).get('message') or ev}")

        return self._send(self._url("/messages"), self._headers(), payload, model, handle, on_token, on_thinking,
                          opts.short, can_omit_max=False)

    def list_models(self) -> list[str]:
        self._need_key()
        try:
            r = self.client.get(self._url("/models"), headers={**self._headers(), **self.headers}, params={"limit": 100})
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise self._wrap("", e) from e
        return [m["id"] for m in r.json().get("data", []) if m.get("id")]


class OpenAICompatProvider(_Online):
    label = "在线模型"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "content-type": "application/json"}

    def chat(self, model: str, messages: list[dict], opts: ChatOptions,
             on_token: TokenCallback | None = None, on_thinking=None) -> ChatResult:
        payload = self._common({"model": model, "messages": messages, "stream": True}, opts)

        def handle(ev: dict, emit, state) -> None:
            if ev.get("error"):
                raise ModelUnavailableError(f"在线模型返回错误：{_error_message(json.dumps(ev))}")
            for ch in ev.get("choices") or []:
                delta = ch.get("delta") or {}
                # 推理模型（DeepSeek、Qwen、Kimi 等）把思考放在 reasoning_content，OpenRouter 等用 reasoning
                thought = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(thought, str):
                    emit.think(thought)
                emit(delta.get("content") or "")
                if ch.get("finish_reason"):
                    state["finish"] = ch["finish_reason"]
            usage = ev.get("usage") or {}
            if usage:
                state["in"] = usage.get("prompt_tokens", state["in"])
                state["out"] = usage.get("completion_tokens", state["out"])

        return self._send(f"{self.base_url}/chat/completions", self._headers(), payload, model, handle, on_token,
                          on_thinking, opts.short, can_omit_max=True)

    def list_models(self) -> list[str]:
        self._need_key()
        try:
            r = self.client.get(f"{self.base_url}/models", headers={**self._headers(), **self.headers})
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise self._wrap("", e) from e
        return sorted(m["id"] for m in r.json().get("data", []) if m.get("id"))


def online_provider(name: str, fmt: str, options: dict, transport=None):
    """按 models.json 里的一项（或 providers 配置）建 provider：网关与“测试连接”共用，参数一致。"""
    cls = AnthropicProvider if fmt == "anthropic" else OpenAICompatProvider
    opts = dict(options)
    kw = {k: opts[k] for k in ("temperature", "max_tokens_param", "extra_body", "extra_body_short", "headers")
          if k in opts}
    return cls(name, base_url=opts.get("base_url") or "", api_key=opts.get("api_key") or "",
               timeout_s=float(opts.get("timeout_s") or 120), transport=transport, **kw)
