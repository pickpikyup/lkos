"""Model Gateway：模型路由、隐私检查、调用日志。

规则（需求文档第 3、8 节）：
1. 模型解析：本次请求的 override 优先，否则用任务默认模型；
2. 远端（在线）模型在 remote_enabled=false 或 offline_mode=true 时被拒绝（在线模型在界面“模型”页配置）；
3. 调用失败直接抛出 ModelUnavailableError，绝不自动换模型（尤其不回退到远端）；
4. 无论成功失败都写调用日志。
"""

from __future__ import annotations

import inspect
import time
from typing import Callable, Protocol

import psutil

from lkos.logs import get_logger
from lkos.config import Config, ProviderConfig, split_model_ref
from lkos.errors import ConfigError, EmbeddingMismatchError, LkosError, ModelUnavailableError, RemoteDisabledError
from lkos.gateway.base import ChatOptions, ChatResult, ThinkingCallback, TokenCallback
from lkos.types import CallLog


_rlog = get_logger("model")

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
    if pc.type in ("anthropic", "openai"):
        from lkos.gateway.online import online_provider

        return online_provider(pc.name, pc.type, opts, transport=transport)
    if pc.type == "hermes":
        from lkos.gateway.hermes import HermesProvider

        return HermesProvider(pc.name, **opts)
    if pc.type == "fake":
        from lkos.gateway.fake import FakeProvider

        opts.setdefault("dim", embedding_dim)
        return FakeProvider(pc.name, **opts)
    raise ConfigError(f"不支持的 provider 类型：{pc.type}")


def _accepts(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def out_of_budget_message(result: ChatResult, max_tokens: int | None = None, remote: bool = True) -> str:
    """输出上限用完、没写出正文时给用户看的说明（写明能照着做的办法）。"""
    t = len(getattr(result, "thinking", "") or "")
    why = f"都用在了思考上（思考了 {t} 字）" if t else "用完了"
    if not remote:
        return f"本机模型的输出上限（{max_tokens} tokens）{why}，没有写出回答。可以换一个不带思考的本机模型。"
    if max_tokens:
        return (f"模型的输出上限（{max_tokens} tokens）{why}，没有写出回答。请在左栏“模型”里点这个模型的“编辑”，"
                "把“输出上限”调大或清空（清空 = 按服务商默认）后重试。")
    return (f"按服务商默认的输出上限，模型的输出仍然{why}，没有写出回答。可以在左栏“模型”→“编辑配置文件”里给这个模型加 "
            "\"extra_body\": {\"reasoning_effort\": \"low\"} 之类的参数降低思考强度（具体参数以服务商文档为准），或换一个模型。")


class ModelGateway:
    def ensure_answer(self, result: ChatResult, ref: str) -> None:
        """回答正文为空、且输出上限被用完时报错（说明怎么调），不给用户一个空回答。"""
        if not result.out_of_budget:
            return
        pname, _ = split_model_ref(ref)
        pc = self.cfg.providers.get(pname)
        remote = bool(pc and pc.is_remote)
        mcfg = self.cfg.chat_model(ref)
        limit = mcfg.max_output if remote else (mcfg.max_output or self.cfg.tasks["knowledge_qa"].max_tokens)
        raise ModelUnavailableError(out_of_budget_message(result, limit, remote))

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
                raise RemoteDisabledError("离线模式已开启，不能使用在线模型（在左栏“模型”里可以关闭离线模式）。")
            if not self.cfg.privacy.remote_enabled:
                raise RemoteDisabledError("在线模型没有启用：请在左栏“模型”里配置并启用。")

    # ---------- chat ----------
    def chat(self, task: str, messages: list[dict], model_override: str | None = None,
             on_token: TokenCallback | None = None, max_tokens: int | None = None,
             log_as: str | None = None, on_thinking: ThinkingCallback | None = None,
             short: bool = False) -> tuple[ChatResult, str]:
        """返回 (结果, 实际使用的模型引用)。
        max_tokens：本机模型的输出上限（覆盖任务配置）。在线模型一律用模型配置的“输出上限”（留空 = 服务商默认）：
        推理模型的思考也算在输出里，调用方按“回答长度”给的小上限会被思考占满；回答长短由提示词控制。
        log_as：调用日志里的任务名；on_thinking：推理模型的思考过程（provider 支持时才传）；
        short：简短的内部调用（在线模型可附加 extra_body_short，例如关闭思考）。"""
        ref = self.resolve(task, model_override)
        pname, model = split_model_ref(ref)
        self._check_privacy(pname)
        provider = self.provider(pname)
        mcfg = self.cfg.chat_model(ref)
        tcfg = self.cfg.tasks.get(task)
        pc = self.cfg.providers.get(pname)
        remote = bool(pc and pc.is_remote)
        opts = ChatOptions(
            temperature=tcfg.temperature if tcfg else 0.3,
            max_tokens=mcfg.max_output if remote else (max_tokens or mcfg.max_output or (tcfg.max_tokens if tcfg else 1024)),
            num_ctx=mcfg.context_window,
            think=mcfg.think,
            short=short,
        )
        task = log_as or task
        try:
            kw = {"on_thinking": on_thinking} if on_thinking and _accepts(provider.chat, "on_thinking") else {}
            result = provider.chat(model, messages, opts, on_token=on_token, **kw)
        except LkosError as e:
            self._log(task, pname, model, error=str(e))
            raise
        except Exception as e:  # noqa: BLE001
            self._log(task, pname, model, error=f"{type(e).__name__}: {e}")
            raise ModelUnavailableError(f"调用 {ref} 失败：{e}") from e
        thinking = getattr(result, "thinking", "") or ""
        finish = getattr(result, "finish_reason", None)
        if thinking or finish not in (None, "stop"):
            _rlog.debug("模型 %s/%s（%s）：思考 %d 字、结束原因 %s", pname, model, task, len(thinking), finish or "-")
        if not (result.text or "").strip() and finish == "length":
            _rlog.warning("模型 %s/%s（%s）的输出上限（%s）用完了，没有写出正文%s", pname, model, task,
                          f"{opts.max_tokens} tokens" if opts.max_tokens else "服务商默认", 
                          f"（都花在了 {len(thinking)} 字的思考上）" if thinking else "")
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
        if fields.get("error") == "已停止生成":
            _rlog.info("用户停止了 %s/%s 的生成", provider, model)
        elif fields.get("error"):
            _rlog.warning("模型调用失败 %s/%s（%s）：%s", provider, model, task, fields["error"])
        elif task != "embed":
            _rlog.debug("模型调用 %s/%s（%s）：%s", provider, model, task,
                        "、".join(f"{k}={v}" for k, v in fields.items() if v is not None))
        if self.logger is None:
            return
        mem, swap = self.snapshot()
        self.logger.log_call(CallLog(task=task, provider=provider, model=model,
                                     mem_available_mb=mem, swap_used_mb=swap, **fields))
