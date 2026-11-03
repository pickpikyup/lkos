"""读取并校验配置：内置默认值 + 用户 config.yaml 深度合并。"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from lkos.errors import ConfigError

KNOWN_PROVIDER_TYPES = {"ollama", "hermes", "fake"}
REMOTE_PROVIDER_TYPES = {"hermes"}


def default_home() -> Path:
    """数据目录：优先环境变量 LKOS_HOME，否则为项目根目录下的 data/。

    设计文档原定 ~/Library/Application Support/LocalKnowledgeOS/；
    POC 阶段按要求把所有数据留在项目文件夹内。
    """
    env = os.environ.get("LKOS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "data").resolve()


@dataclass
class ProviderConfig:
    name: str
    type: str
    options: dict[str, Any]

    @property
    def is_remote(self) -> bool:
        return self.type in REMOTE_PROVIDER_TYPES


@dataclass
class ChatModelConfig:
    ref: str
    context_window: int = 8192
    think: bool = False


@dataclass
class EmbeddingConfig:
    ref: str
    dim: int
    batch_size: int = 16


@dataclass
class TaskConfig:
    name: str
    default_model: str
    temperature: float = 0.3
    max_tokens: int = 1024


STRATEGIES = ("doc_first", "flat")


@dataclass
class IndexingConfig:
    extensions: list[str] = field(default_factory=lambda: [".pdf", ".docx", ".md"])
    max_file_size_mb: float = 100
    chunk_chars_zh: int = 500            # 中文为主的段落：每块约 500 字
    chunk_chars_other: int = 1200        # 英文、法文等：每块约 1,200 字符（token 数与中文 500 字接近）
    chunk_overlap_ratio: float = 0.15
    chunk_max_chars: int | None = None   # 旧写法：所有语言同一长度（v0.2 为 800），设置后覆盖上面三项
    chunk_overlap_chars: int = 100       # 仅与 chunk_max_chars 一起使用
    skip_hidden: bool = True
    order: str = "recent_first"          # recent_first：最近修改的文件先索引 | path：按路径
    idle_seconds: float = 120            # --when-idle：用户无操作多久后才索引
    require_ac_power: bool = True        # --when-idle：使用电池时暂停
    poll_seconds: float = 5


@dataclass
class RetrievalConfig:
    top_k: int = 4
    candidates_per_method: int = 30
    rrf_k: int = 60
    language_filter: str = "soft"   # soft | strict | all（auto 为旧写法，等同 strict）
    same_lang_boost: float = 1.2    # soft 模式下同语言文本块的融合分数倍率
    vector_candidates: int = 200    # 二值粗筛取多少候选再做 int8 精排
    strategy: str = "doc_first"     # doc_first：先选文档再选段落（M1）| flat：直接取融合分最高的块（v0.2）
    doc_score_top: int = 3          # 文档得分 = 该文档最好的几个块的分数之和
    per_doc_max: int = 3            # 同一文档最多送几个片段
    merge_adjacent: bool = True     # 同一文件相邻的片段合并为一个
    neighbor_ratio: float = 1.5     # 最佳片段分数 ≥ 第二名的多少倍时补入前后相邻块；0 = 关闭


@dataclass
class PrivacyConfig:
    remote_enabled: bool = False
    offline_mode: bool = True


@dataclass
class Config:
    providers: dict[str, ProviderConfig]
    chat_models: dict[str, ChatModelConfig]
    embedding: EmbeddingConfig
    tasks: dict[str, TaskConfig]
    indexing: IndexingConfig
    retrieval: RetrievalConfig
    privacy: PrivacyConfig

    def chat_model(self, ref: str) -> ChatModelConfig:
        """未在 models.chat 中登记的模型使用默认参数（允许用户临时覆盖）。"""
        return self.chat_models.get(ref) or ChatModelConfig(ref=ref)


def split_model_ref(ref: str) -> tuple[str, str]:
    """'local/qwen3:4b' -> ('local', 'qwen3:4b')。"""
    if not isinstance(ref, str) or "/" not in ref:
        raise ConfigError(f"模型引用必须是 '<provider>/<model>' 格式：{ref!r}")
    provider, model = ref.split("/", 1)
    if not provider or not model:
        raise ConfigError(f"模型引用必须是 '<provider>/<model>' 格式：{ref!r}")
    return provider, model


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def default_raw() -> dict:
    text = resources.files("lkos").joinpath("config.default.yaml").read_text("utf-8")
    return yaml.safe_load(text)


def load_config(path: Path | None = None, overrides: dict | None = None) -> Config:
    raw = default_raw()
    if path is not None and Path(path).exists():
        try:
            user = yaml.safe_load(Path(path).read_text("utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"配置文件不是合法 YAML：{path}：{e}") from e
        if not isinstance(user, dict):
            raise ConfigError(f"配置文件顶层必须是映射：{path}")
        raw = deep_merge(raw, user)
    if overrides:
        raw = deep_merge(raw, overrides)
    return parse_config(raw)


def parse_config(raw: dict) -> Config:
    try:
        providers = {}
        for name, opts in (raw.get("providers") or {}).items():
            opts = dict(opts or {})
            ptype = opts.pop("type", None)
            if ptype not in KNOWN_PROVIDER_TYPES:
                raise ConfigError(f"providers.{name}.type 不支持：{ptype!r}")
            providers[name] = ProviderConfig(name=name, type=ptype, options=opts)

        models = raw.get("models") or {}
        chat_models = {
            ref: ChatModelConfig(
                ref=ref,
                context_window=int((v or {}).get("context_window", 8192)),
                think=bool((v or {}).get("think", False)),
            )
            for ref, v in (models.get("chat") or {}).items()
        }
        emb = models.get("embedding") or {}
        embedding = EmbeddingConfig(
            ref=str(emb["ref"]), dim=int(emb["dim"]), batch_size=int(emb.get("batch_size", 16))
        )

        tasks = {}
        for name, t in (raw.get("tasks") or {}).items():
            t = t or {}
            tasks[name] = TaskConfig(
                name=name,
                default_model=str(t["default_model"]),
                temperature=float(t.get("temperature", 0.3)),
                max_tokens=int(t.get("max_tokens", 1024)),
            )

        idx = raw.get("indexing") or {}
        indexing = IndexingConfig(
            extensions=[e.lower() if e.startswith(".") else "." + e.lower() for e in idx.get("extensions", [".pdf", ".docx", ".md"])],
            max_file_size_mb=float(idx.get("max_file_size_mb", 100)),
            chunk_chars_zh=int(idx.get("chunk_chars_zh", 500)),
            chunk_chars_other=int(idx.get("chunk_chars_other", 1200)),
            chunk_overlap_ratio=float(idx.get("chunk_overlap_ratio", 0.15)),
            chunk_max_chars=None if idx.get("chunk_max_chars") is None else int(idx["chunk_max_chars"]),
            chunk_overlap_chars=int(idx.get("chunk_overlap_chars", 100)),
            skip_hidden=bool(idx.get("skip_hidden", True)),
            order=str(idx.get("order", "recent_first")),
            idle_seconds=float(idx.get("idle_seconds", 120)),
            require_ac_power=bool(idx.get("require_ac_power", True)),
            poll_seconds=float(idx.get("poll_seconds", 5)),
        )
        ret = raw.get("retrieval") or {}
        retrieval = RetrievalConfig(
            top_k=int(ret.get("top_k", 4)),
            candidates_per_method=int(ret.get("candidates_per_method", 30)),
            rrf_k=int(ret.get("rrf_k", 60)),
            language_filter=str(ret.get("language_filter", "soft")),
            same_lang_boost=float(ret.get("same_lang_boost", 1.2)),
            vector_candidates=int(ret.get("vector_candidates", 200)),
            strategy=str(ret.get("strategy", "doc_first")),
            doc_score_top=int(ret.get("doc_score_top", 3)),
            per_doc_max=int(ret.get("per_doc_max", 3)),
            merge_adjacent=bool(ret.get("merge_adjacent", True)),
            neighbor_ratio=float(ret.get("neighbor_ratio", 1.5)),
        )
        pv = raw.get("privacy") or {}
        privacy = PrivacyConfig(
            remote_enabled=bool(pv.get("remote_enabled", False)),
            offline_mode=bool(pv.get("offline_mode", True)),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"配置缺少字段或类型错误：{e}") from e

    cfg = Config(providers, chat_models, embedding, tasks, indexing, retrieval, privacy)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    for ref in [cfg.embedding.ref, *[t.default_model for t in cfg.tasks.values()]]:
        provider, _ = split_model_ref(ref)
        if provider not in cfg.providers:
            raise ConfigError(f"模型 {ref} 引用了未定义的 provider：{provider}")
    if cfg.embedding.dim <= 0:
        raise ConfigError("models.embedding.dim 必须为正整数")
    ix = cfg.indexing
    if ix.chunk_max_chars is not None:
        if ix.chunk_overlap_chars >= ix.chunk_max_chars:
            raise ConfigError("chunk_overlap_chars 必须小于 chunk_max_chars")
    else:
        if min(ix.chunk_chars_zh, ix.chunk_chars_other) < 50:
            raise ConfigError("chunk_chars_zh / chunk_chars_other 不能小于 50")
        if not 0 <= ix.chunk_overlap_ratio < 0.5:
            raise ConfigError("chunk_overlap_ratio 必须在 0 到 0.5 之间")
    r = cfg.retrieval
    if r.strategy not in STRATEGIES:
        raise ConfigError(f"retrieval.strategy 只能是 {' 或 '.join(STRATEGIES)}")
    if r.doc_score_top < 1 or r.per_doc_max < 1:
        raise ConfigError("retrieval.doc_score_top 与 per_doc_max 必须 ≥ 1")
    if r.neighbor_ratio < 0:
        raise ConfigError("retrieval.neighbor_ratio 不能为负数（0 表示关闭补邻）")
    if cfg.indexing.order not in ("recent_first", "path"):
        raise ConfigError("indexing.order 只能是 recent_first 或 path")
    if cfg.retrieval.language_filter not in ("soft", "strict", "all", "auto"):
        raise ConfigError("retrieval.language_filter 只能是 soft、strict 或 all")
    if cfg.retrieval.same_lang_boost < 1:
        raise ConfigError("retrieval.same_lang_boost 不能小于 1")
    if cfg.retrieval.vector_candidates < cfg.retrieval.candidates_per_method:
        raise ConfigError("retrieval.vector_candidates 不能小于 candidates_per_method")
    if "knowledge_qa" not in cfg.tasks:
        raise ConfigError("tasks.knowledge_qa 必须配置")


def make_chunker(ix: IndexingConfig):
    """按配置创建切块器：设置了旧写法 chunk_max_chars 时所有语言同一长度，否则按语言区分。"""
    from lkos.indexing.chunker import Chunker

    if ix.chunk_max_chars is not None:
        return Chunker(ix.chunk_max_chars, ix.chunk_overlap_chars)
    return Chunker(max_chars_by_lang={"zh": ix.chunk_chars_zh, "other": ix.chunk_chars_other},
                   overlap_ratio=ix.chunk_overlap_ratio)
