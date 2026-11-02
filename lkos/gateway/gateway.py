"""Model Gateway：模型路由、隐私检查、调用日志。

规则（需求文档第 3、8 节）：
1. 模型解析：本次请求的 override 优先，否则用任务默认模型；
2. 远端模型在 remote_enabled=false 或 offline_mode=true 时被拒绝；
3. 调用失败直接抛出 ModelUnavailableError，绝不自动换模型（尤其不回退到远端）；
4. 无论成功失败都写调用日志。
"""

from __future__ import annotations

import time
from typing import Callable, Protocol

import psutil

from lkos.config import Config, ProviderConfig, split_model_ref
from lkos.errors import ConfigError, EmbeddingMismatchError, LkosError, ModelUnavailableError, RemoteDisabledError
from lkos.gateway.base import ChatOptions, ChatResult, TokenCallback
from lkos.types import CallLog


class CallLogger(Protocol):
    def log_call(self, entry: CallLog) -> None: ...


def memory_snapshot() -> tuple[int | None, int | None]:
    try:
        return (int(psutil.virtual_memory().available / 2**20), int(psutil.swap_memory().used / 2**20))
    except Exception:  # noqa: BLE001 - 某些沙箱环境读不到交换区
        return (None, None)


def build_provider(pc: ProviderConfig, embedding_dim: int, transport=None):
    opts = dict(pc.options)
    if pc.type == "ollama":
        from lkos.gateway.ollama import OllamaProvider

        return OllamaProvider(
            pc.name, base_url=opts.get("base_url") or "http://127.0.0.1:11434",
            keep_alive=str(opts.get("keep_alive", "30m")), timeout_s=float(opts.get("timeout_s", 120)),
            transport=transport,
        )
    if pc.type == "hermes":
        from lkos.gateway.hermes import HermesProvider

        return HermesProvider(pc.name, **opts)
    if pc.type == "fake":
        from lkos.gateway.fake import FakeProvider

        opts.setdefault("dim", embedding_dim)
        return FakeProvider(pc.name, **opts)
    raise ConfigError(f"不支持的 provider 类型：{pc.type}")


class ModelGateway:
    def __init__(self, cfg: Config, logger: CallLogger | None = None, providers: dict | None = None,
                 snapshot: Callable[[], tuple[int | None, int | None]] = memory_snapshot):
        self.cfg = cfg
        self.logger = logger
        self.snapshot = snapshot
        self._providers: dict = dict(providers or {})

    def provider(self, name: str):
        if name not in self._providers:
            pc = self.cfg.providers.get(name)
            if pc is None:
                raise ConfigError(f"未定义的 provider：{name}")
            self._providers[name] = build_provider(pc, self.cfg.embedding.dim)
        return self._providers[name]

    # ---------- 路由 ----------
    def resolve(self, task: str, model_override: str | None = None) -> str:
        if model_override:
            return model_override
        t = self.cfg.tasks.get(task)
        if t is None:
            raise ConfigError(f"未配置任务：{task}")
        return t.default_model

    def _check_privacy(self, provider_name: str) -> None:
        pc = self.cfg.providers.get(provider_name)
        if pc is None:
            raise ConfigError(f"未定义的 provider：{provider_name}")
        if pc.is_remote:
            if self.cfg.privacy.offline_mode:
                raise RemoteDisabledError("离线模式已开启，禁止调用远端模型。")
            if not self.cfg.privacy.remote_enabled:
                raise RemoteDisabledError("远端调用未开启（privacy.remote_enabled=false）。")

    # ---------- chat ----------
    def chat(self, task: str, messages: list[dict], model_override: str | None = None,
             on_token: TokenCallback | None = None) -> tuple[ChatResult, str]:
        """返回 (结果, 实际使用的模型引用)。"""
        ref = self.resolve(task, model_override)
        pname, model = split_model_ref(ref)
        self._check_privacy(pname)
        provider = self.provider(pname)
        mcfg = self.cfg.chat_model(ref)
        tcfg = self.cfg.tasks.get(task)
        opts = ChatOptions(
            temperature=tcfg.temperature if tcfg else 0.3,
            max_tokens=tcfg.max_tokens if tcfg else 1024,
            num_ctx=mcfg.context_window,
            think=mcfg.think,
        )
        try:
            result = provider.chat(model, messages, opts, on_token=on_token)
        except LkosError as e:
            self._log(task, pname, model, error=str(e))
            raise
        except Exception as e:  # noqa: BLE001
            self._log(task, pname, model, error=f"{type(e).__name__}: {e}")
            raise ModelUnavailableError(f"调用 {ref} 失败：{e}") from e
        self._log(task, pname, model, latency_ms=result.latency_ms, ttft_ms=result.ttft_ms,
                  input_tokens=result.input_tokens, output_tokens=result.output_tokens)
        return result, ref

    # ---------- embed ----------
    def embed(self, texts: list[str], task: str = "embed") -> list[list[float]]:
        ref = self.cfg.embedding.ref
        pname, model = split_model_ref(ref)
        self._check_privacy(pname)
        provider = self.provider(pname)
        start = time.perf_counter()
        try:
            vectors = provider.embed(model, texts)
        except LkosError as e:
            self._log(task, pname, model, error=str(e))
            raise
        except Exception as e:  # noqa: BLE001
            self._log(task, pname, model, error=f"{type(e).__name__}: {e}")
            raise ModelUnavailableError(f"调用 {ref} 失败：{e}") from e
        dim = self.cfg.embedding.dim
        if any(len(v) != dim for v in vectors):
            got = len(vectors[0]) if vectors else 0
            err = f"Embedding 维度为 {got}，与配置 models.embedding.dim={dim} 不一致"
            self._log(task, pname, model, error=err)
            raise EmbeddingMismatchError(err)
        self._log(task, pname, model, latency_ms=int((time.perf_counter() - start) * 1000),
                  input_tokens=sum(len(t) for t in texts) // 2)
        return vectors

    def check(self) -> list[tuple[str, bool, str]]:
        """检查任务默认模型与 Embedding 模型是否可用（lkos init 使用）。"""
        refs = [self.cfg.embedding.ref] + sorted({t.default_model for t in self.cfg.tasks.values()})
        out = []
        for ref in refs:
            pname, model = split_model_ref(ref)
            try:
                self._check_privacy(pname)
                res = self.provider(pname).check_model(model)
                out.append((ref, res.ok, res.message))
            except LkosError as e:
                out.append((ref, False, str(e)))
        return out

    def _log(self, task: str, provider: str, model: str, **fields) -> None:
        if self.logger is None:
            return
        mem, swap = self.snapshot()
        self.logger.log_call(CallLog(task=task, provider=provider, model=model,
                                     mem_available_mb=mem, swap_used_mb=swap, **fields))
