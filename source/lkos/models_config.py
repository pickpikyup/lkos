"""模型配置（界面左栏“模型”页）：在线模型、本机模型、向量模型，保存在 <数据目录>/models.json。

- 当前版本密钥明文保存在 models.json（data/ 不进 Git）；以后有了用户登录再改为加密保存。
- 读取顺序：内置默认 < config.yaml < models.json < 程序传入的覆盖（测试用）。
- 在线模型有两种接口格式：anthropic（Claude 的 Messages API）与 openai（OpenAI 兼容的 /chat/completions，
  DeepSeek、通义千问、Kimi、智谱、Gemini、OpenRouter 等都支持）。
- 每个在线模型除了界面上的几项，还可以在“编辑配置文件”里写高级参数（ADVANCED_KEYS），原样传给接口；
  不认识的字段也原样保留，不会因为在界面上保存而丢失。
- 文件写错时不当作“没有配置”：记下错误、界面提示去修正，期间界面上的保存一律拒绝，免得覆盖掉原文件。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

MODELS_FILE = "models.json"
ONLINE_PREFIX = "on-"
MODELS_VERSION = 2            # 2：max_tokens 可以留空（= 服务商默认）；旧版本里的 4096 是程序默认值，迁移为留空
LEGACY_DEFAULT_MAX = 4096
ANTHROPIC_DEFAULT_MAX = 16000  # Claude 接口必须给 max_tokens：没写时用这个
ONLINE_RESERVE = 8192          # 输出上限留空时，按这么多给回答预留上下文（最多占上下文的 1/4）

# 在线模型的高级参数（在“编辑配置文件”里写）：
ADVANCED_KEYS = {
    "temperature": "数字；写 null 表示不传（有的推理模型不接受温度参数）；不写则用任务默认值",
    "max_tokens_param": "输出上限的参数名：max_tokens（默认）或 max_completion_tokens（OpenAI 较新的模型）",
    "extra_body": "对象：合并进每次请求的请求体，例如 {\"reasoning_effort\": \"low\"}",
    "extra_body_short": "对象：只合并进拆方面、写关键词、生成标签这类简短的内部调用，例如关闭思考 "
                        "{\"thinking\": {\"type\": \"disabled\"}}；写 {} 表示不加",
    "headers": "对象：额外的 HTTP 请求头",
    "timeout_s": "数字：单次请求超时秒数（默认 120）",
}

# 预置的在线服务（group：vendor 模型厂商 / aggregator 第三方聚合与推理服务）。
# models 只是填写示例，以“获取模型列表”拿到的为准
PRESETS: list[dict] = [
    {"id": "anthropic", "name": "Claude（Anthropic）", "format": "anthropic", "base_url": "https://api.anthropic.com",
     "web": "https://console.anthropic.com/settings/keys", "key_hint": "sk-ant-…",
     "models": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"], "context_window": 200000},
    {"id": "openai", "name": "OpenAI", "format": "openai", "base_url": "https://api.openai.com/v1",
     "web": "https://platform.openai.com/api-keys", "key_hint": "sk-…", "models": [], "context_window": 128000},
    {"id": "deepseek", "name": "DeepSeek", "format": "openai", "base_url": "https://api.deepseek.com/v1",
     "web": "https://platform.deepseek.com/api_keys", "key_hint": "sk-…",
     "models": ["deepseek-chat", "deepseek-reasoner"], "context_window": 64000},
    {"id": "qwen", "name": "通义千问（阿里云百炼）", "format": "openai",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "web": "https://bailian.console.aliyun.com/?apiKey=1", "key_hint": "sk-…",
     "models": ["qwen-plus", "qwen-max", "qwen-turbo"], "context_window": 128000},
    {"id": "kimi", "name": "Kimi（月之暗面）", "format": "openai", "base_url": "https://api.moonshot.cn/v1",
     "web": "https://platform.moonshot.cn/console/api-keys", "key_hint": "sk-…",
     "models": ["moonshot-v1-128k"], "context_window": 128000},
    {"id": "zhipu", "name": "智谱 GLM", "format": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "web": "https://open.bigmodel.cn/usercenter/apikeys", "key_hint": "…", "models": ["glm-4-plus"],
     "context_window": 128000},
    {"id": "gemini", "name": "Google Gemini", "format": "openai",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "web": "https://aistudio.google.com/apikey", "key_hint": "AIza…", "models": [], "context_window": 1000000},
    # ---- 第三方聚合 / 推理服务：一个密钥可以调用多家的模型（都用 OpenAI 兼容接口）----
    {"id": "openrouter", "group": "aggregator", "name": "OpenRouter", "format": "openai",
     "base_url": "https://openrouter.ai/api/v1", "web": "https://openrouter.ai/keys", "key_hint": "sk-or-…",
     "models": [], "context_window": 128000},
    {"id": "nous", "group": "aggregator", "name": "Nous Research（Nous Portal）", "format": "openai",
     "base_url": "https://inference-api.nousresearch.com/v1", "web": "https://portal.nousresearch.com",
     "key_hint": "…", "models": ["Hermes-4-70B", "Hermes-4-405B"], "context_window": 128000},
    {"id": "together", "group": "aggregator", "name": "Together AI", "format": "openai",
     "base_url": "https://api.together.xyz/v1", "web": "https://api.together.xyz/settings/api-keys",
     "key_hint": "…", "models": [], "context_window": 128000},
    {"id": "groq", "group": "aggregator", "name": "Groq", "format": "openai",
     "base_url": "https://api.groq.com/openai/v1", "web": "https://console.groq.com/keys", "key_hint": "gsk_…",
     "models": [], "context_window": 128000},
    {"id": "fireworks", "group": "aggregator", "name": "Fireworks AI", "format": "openai",
     "base_url": "https://api.fireworks.ai/inference/v1", "web": "https://fireworks.ai/account/api-keys",
     "key_hint": "fw_…", "models": [], "context_window": 128000},
    {"id": "deepinfra", "group": "aggregator", "name": "DeepInfra", "format": "openai",
     "base_url": "https://api.deepinfra.com/v1/openai", "web": "https://deepinfra.com/dash/api_keys",
     "key_hint": "…", "models": [], "context_window": 128000},
    {"id": "siliconflow", "group": "aggregator", "name": "硅基流动 SiliconFlow", "format": "openai",
     "base_url": "https://api.siliconflow.cn/v1", "web": "https://cloud.siliconflow.cn/account/ak",
     "key_hint": "sk-…", "models": [], "context_window": 128000},
    {"id": "volcengine", "group": "aggregator", "name": "火山引擎方舟", "format": "openai",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3", "web": "https://console.volcengine.com/ark",
     "key_hint": "…", "models": [], "context_window": 128000},
    {"id": "mistral", "name": "Mistral AI", "format": "openai", "base_url": "https://api.mistral.ai/v1",
     "web": "https://console.mistral.ai/api-keys", "key_hint": "…", "models": [], "context_window": 128000},
    {"id": "xai", "name": "xAI（Grok）", "format": "openai", "base_url": "https://api.x.ai/v1",
     "web": "https://console.x.ai", "key_hint": "xai-…", "models": [], "context_window": 128000},
]
PRESET_GROUPS = {"vendor": "模型厂商", "aggregator": "第三方聚合 / 推理服务（一个密钥用多家模型）"}

# 自定义接口的填写说明与示例（界面上显示）
CUSTOM_GUIDE = {
    "name": {"label": "名称", "hint": "自己起的名字，显示在模型下拉框里", "example": "公司内网大模型"},
    "base_url": {"label": "API 地址", "hint": "接口的基础地址，不含 /chat/completions 或 /v1/messages",
                 "example": "https://api.deepseek.com/v1"},
    "web": {"label": "网站（可选）", "hint": "申请密钥、查看用量的网页，方便以后找", "example": "https://platform.deepseek.com"},
    "format": {"label": "接口格式", "hint": "大多数国内外服务选“OpenAI 兼容”；Claude 官方接口选“Anthropic”",
               "example": "OpenAI 兼容"},
    "api_key": {"label": "API 密钥", "hint": "在服务商网站上创建；本机明文保存在 data/models.json（不进 Git）",
                "example": "sk-xxxxxxxx"},
    "model": {"label": "模型名", "hint": "服务商文档里的模型 ID；填好地址和密钥后可点“获取模型列表”",
              "example": "deepseek-chat"},
    "context_window": {"label": "上下文长度（token）", "hint": "一次最多能输入多少（资料 + 对话）；不清楚就填 32000",
                       "example": "64000"},
    "max_tokens": {"label": "输出上限（token）", "hint": "一次最多输出多少，推理模型的思考也算在内；留空 = 按服务商默认（推荐）",
                   "example": "留空"},
}

DEFAULT_LOCAL = {"base_url": "http://127.0.0.1:11434", "chat": "qwen3:4b-instruct", "context_window": 4096}
DEFAULT_EMBEDDING = {"model": "bge-m3", "dim": 1024}


def default_models() -> dict:
    return {"local": dict(DEFAULT_LOCAL), "embedding": dict(DEFAULT_EMBEDDING), "online": [],
            "offline_mode": False, "consent": {}}


def models_path(home: Path) -> Path:
    return Path(home) / MODELS_FILE


class ModelsFileError(ValueError):
    """models.json 内容有错（格式不对、字段类型不对）。"""


def parse_models(data) -> dict:
    """已解析的 JSON / YAML 内容 → 规范化的模型配置；有错时抛 ModelsFileError（写明是哪一项）。"""
    if not isinstance(data, dict):
        raise ModelsFileError("顶层必须是一个对象（{ ... }）")
    legacy = int(data.get("version") or 1) < MODELS_VERSION
    out = default_models()
    out.update({k: v for k, v in data.items() if k in out or k not in ("version",)})
    for key in ("local", "embedding"):
        if data.get(key) is not None and not isinstance(data.get(key), dict):
            raise ModelsFileError(f"{key} 必须是对象")
    out["local"] = {**DEFAULT_LOCAL, **(data.get("local") or {})}
    out["embedding"] = {**DEFAULT_EMBEDDING, **(data.get("embedding") or {})}
    online = data.get("online") or []
    if not isinstance(online, list):
        raise ModelsFileError("online 必须是列表（[ ... ]）")
    items, seen = [], set()
    for i, m in enumerate(online, start=1):
        if not isinstance(m, dict):
            raise ModelsFileError(f"online 第 {i} 项必须是对象")
        try:
            n = normalize_online(m, legacy=legacy)
        except (TypeError, ValueError) as e:
            raise ModelsFileError(f"online 第 {i} 项（{m.get('name') or m.get('id') or m.get('model') or '?'}）：{e}") from e
        if n["id"] in seen:
            raise ModelsFileError(f"online 第 {i} 项的 id“{n['id']}”和前面的重复")
        seen.add(n["id"])
        items.append(n)
    out["online"] = items
    if not isinstance(out.get("consent") or {}, dict):
        raise ModelsFileError("consent 必须是对象")
    out["offline_mode"] = bool(out.get("offline_mode"))
    return out


def load_models_checked(home: Path) -> tuple[dict | None, str | None]:
    """返回 (配置, 错误)。没有 models.json：(None, None)；文件有错：(None, 错误说明)。"""
    p = models_path(home)
    if not p.exists():
        return None, None
    try:
        text = p.read_text("utf-8")
    except OSError as e:
        return None, f"读不了 {p.name}：{e}"
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, f"{p.name} 不是有效的 JSON（第 {getattr(e, 'lineno', '?')} 行第 {getattr(e, 'colno', '?')} 列）：{getattr(e, 'msg', e)}"
    try:
        return parse_models(data), None
    except ModelsFileError as e:
        return None, f"{p.name}：{e}"


def load_models(home: Path) -> dict | None:
    """没有 models.json、或文件有错时返回 None（沿用 config.yaml 与内置默认；错误用 load_models_checked 取）。"""
    return load_models_checked(home)[0]


def save_models(home: Path, data: dict) -> None:
    p = models_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    data = {"version": MODELS_VERSION, **{k: v for k, v in data.items() if k != "version"}}
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    try:
        tmp.chmod(0o600)          # 只有自己能读
    except OSError:
        pass
    tmp.replace(p)


def new_id(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "model"
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}-{n}", n + 1
    return cid


def _opt_int(v, name: str, low: int = 1) -> int | None:
    """空 / null / 0 → None；否则必须是 ≥ low 的整数。"""
    if v in (None, "", 0, "0"):
        return None
    if isinstance(v, bool):
        raise ValueError(f"{name} 必须是整数")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是整数，现在是 {v!r}") from None
    if n < low:
        raise ValueError(f"{name} 不能小于 {low}")
    return n


def _check_advanced(m: dict) -> dict:
    out = {}
    for key in ("extra_body", "extra_body_short", "headers"):
        if key in m and m[key] is not None:
            if not isinstance(m[key], dict):
                raise ValueError(f"{key} 必须是对象（{{ ... }}）")
            out[key] = m[key]
    if "headers" in out and not all(isinstance(v, str) for v in out["headers"].values()):
        raise ValueError("headers 里的值必须是字符串")
    if "temperature" in m:
        t = m["temperature"]
        if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float))):
            raise ValueError("temperature 必须是数字或 null")
        out["temperature"] = t
    if "max_tokens_param" in m and m["max_tokens_param"] is not None:
        if m["max_tokens_param"] not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("max_tokens_param 只能是 max_tokens 或 max_completion_tokens")
        out["max_tokens_param"] = m["max_tokens_param"]
    if m.get("timeout_s") is not None:
        out["timeout_s"] = _opt_int(m["timeout_s"], "timeout_s", 5)
    return out


BASIC_KEYS = ("id", "preset", "name", "format", "base_url", "web", "api_key", "model", "context_window", "max_tokens",
              "enabled", "added_at")


def normalize_online(m: dict, legacy: bool = False) -> dict:
    """规范化一个在线模型。max_tokens 为 None 表示按服务商默认；高级参数检查类型后保留；不认识的字段原样保留。
    legacy：旧版本文件——当时界面上没有“输出上限”，4096 都是程序默认值，迁移为 None。"""
    fmt = m.get("format") if m.get("format") in ("anthropic", "openai") else "openai"
    max_tokens = _opt_int(m.get("max_tokens"), "max_tokens（输出上限）", 16)
    if legacy and max_tokens == LEGACY_DEFAULT_MAX:
        max_tokens = None
    extra = {k: v for k, v in m.items() if k not in BASIC_KEYS and k not in ADVANCED_KEYS}   # 不认识的字段：原样保留
    adv = _check_advanced(m)
    preset = str(m.get("preset") or "custom")
    # DeepSeek 默认开思考：简短的内部调用（拆方面、关键词、标签）关掉思考，又快又不会被思考占满输出
    # （DeepSeek 文档：thinking: {"type": "enabled/disabled"}）。写了 extra_body_short（哪怕是 {}）就不动
    if preset == "deepseek" and "extra_body_short" not in m:
        adv["extra_body_short"] = {"thinking": {"type": "disabled"}}
    return {
        "id": str(m.get("id") or new_id(str(m.get("name") or m.get("model") or "model"), set())),
        "preset": str(m.get("preset") or "custom"),
        "name": str(m.get("name") or m.get("model") or "在线模型").strip()[:40],
        "format": fmt,
        "base_url": str(m.get("base_url") or "").strip().rstrip("/"),
        "web": str(m.get("web") or "").strip(),
        "api_key": str(m.get("api_key") or "").strip(),
        "model": str(m.get("model") or "").strip(),
        "context_window": max(2048, _opt_int(m.get("context_window"), "context_window（上下文长度）") or 32000),
        "max_tokens": max_tokens,
        "enabled": bool(m.get("enabled", True)),
        "added_at": m.get("added_at") or time.strftime("%Y-%m-%d"),
        **adv,
        **extra,
    }


def online_ref(m: dict) -> str:
    return f"{ONLINE_PREFIX}{m['id']}/{m['model']}"


def is_online_ref(ref: str | None) -> bool:
    return bool(ref) and str(ref).startswith(ONLINE_PREFIX)


def to_overrides(mc: dict) -> dict:
    """models.json → 配置覆盖（providers / models / tasks / privacy）。"""
    local = mc["local"]
    emb = mc["embedding"]
    chat_ref = f"local/{local['chat']}"
    over = {
        "providers": {"local": {"type": "ollama", "base_url": local["base_url"]}},
        "models": {"chat": {chat_ref: {"context_window": int(local["context_window"]), "think": False}},
                   "embedding": {"ref": f"local/{emb['model']}", "dim": int(emb["dim"])}},
        "tasks": {"knowledge_qa": {"default_model": chat_ref}},
    }
    online = [m for m in mc["online"] if m["enabled"] and m["model"] and m["base_url"]]
    for m in online:
        pname = f"{ONLINE_PREFIX}{m['id']}"
        over["providers"][pname] = {"type": m["format"], "base_url": m["base_url"], "api_key": m["api_key"],
                                    **{k: m[k] for k in ADVANCED_KEYS if k in m}}
        over["models"]["chat"][online_ref(m)] = {"context_window": m["context_window"], "think": False,
                                                 "max_output": m["max_tokens"]}
    over["privacy"] = {"remote_enabled": bool(online) and not mc.get("offline_mode"),
                       "offline_mode": bool(mc.get("offline_mode"))}
    return over


def model_choices(mc: dict | None, cfg) -> list[dict]:
    """对话里可选的模型：本机默认模型 + 已启用的在线模型。"""
    local_ref = cfg.tasks["knowledge_qa"].default_model
    out = [{"ref": local_ref, "name": local_ref.split("/", 1)[1], "online": False,
            "context_window": cfg.chat_model(local_ref).context_window}]
    if mc and not mc.get("offline_mode"):
        for m in mc["online"]:
            if m["enabled"] and m["model"] and m["base_url"]:
                out.append({"ref": online_ref(m), "name": m["name"], "online": True, "id": m["id"],
                            "service": m["base_url"], "context_window": m["context_window"],
                            "consented": bool((mc.get("consent") or {}).get(m["id"]))})
    return out


def display_name(ref: str | None, mc: dict | None) -> str:
    """回答底部显示的模型名：本机模型显示模型 ID，在线模型显示配置时起的名字。"""
    if not ref:
        return ""
    if is_online_ref(ref) and mc:
        pid = ref.split("/", 1)[0][len(ONLINE_PREFIX):]
        for m in mc["online"]:
            if m["id"] == pid:
                return m["name"]
    return ref.split("/", 1)[1] if "/" in ref else ref


def public(mc: dict) -> dict:
    """给界面用：密钥只显示末 4 位。"""
    out = json.loads(json.dumps(mc))
    for m in out["online"]:
        k = m.pop("api_key", "")
        m["key_set"] = bool(k)
        m["key_tail"] = k[-4:] if len(k) >= 8 else ""
    return out
