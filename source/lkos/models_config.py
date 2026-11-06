"""模型配置（界面左栏“模型”页）：在线模型、本机模型、向量模型，保存在 <数据目录>/models.json。

- 当前版本密钥明文保存在 models.json（data/ 不进 Git）；以后有了用户登录再改为加密保存。
- 读取顺序：内置默认 < config.yaml < models.json < 程序传入的覆盖（测试用）。
- 在线模型有两种接口格式：anthropic（Claude 的 Messages API）与 openai（OpenAI 兼容的 /chat/completions，
  DeepSeek、通义千问、Kimi、智谱、Gemini、OpenRouter 等都支持）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

MODELS_FILE = "models.json"
ONLINE_PREFIX = "on-"

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
    "context_window": {"label": "上下文长度（token）", "hint": "决定一次能带多少资料和对话；不清楚就填 32000",
                       "example": "64000"},
}

DEFAULT_LOCAL = {"base_url": "http://127.0.0.1:11434", "chat": "qwen3:4b-instruct", "context_window": 4096}
DEFAULT_EMBEDDING = {"model": "bge-m3", "dim": 1024}


def default_models() -> dict:
    return {"local": dict(DEFAULT_LOCAL), "embedding": dict(DEFAULT_EMBEDDING), "online": [],
            "offline_mode": False, "consent": {}}


def models_path(home: Path) -> Path:
    return Path(home) / MODELS_FILE


def load_models(home: Path) -> dict | None:
    """没有 models.json 时返回 None（沿用 config.yaml 与内置默认）。"""
    p = models_path(home)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    out = default_models()
    out.update({k: v for k, v in data.items() if k in out})
    out["local"] = {**DEFAULT_LOCAL, **(data.get("local") or {})}
    out["embedding"] = {**DEFAULT_EMBEDDING, **(data.get("embedding") or {})}
    out["online"] = [normalize_online(m) for m in data.get("online") or [] if isinstance(m, dict)]
    return out


def save_models(home: Path, data: dict) -> None:
    p = models_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
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


def normalize_online(m: dict) -> dict:
    fmt = m.get("format") if m.get("format") in ("anthropic", "openai") else "openai"
    return {
        "id": str(m.get("id") or new_id(str(m.get("name") or m.get("model") or "model"), set())),
        "preset": str(m.get("preset") or "custom"),
        "name": str(m.get("name") or m.get("model") or "在线模型").strip()[:40],
        "format": fmt,
        "base_url": str(m.get("base_url") or "").strip().rstrip("/"),
        "web": str(m.get("web") or "").strip(),
        "api_key": str(m.get("api_key") or "").strip(),
        "model": str(m.get("model") or "").strip(),
        "context_window": max(2048, int(m.get("context_window") or 32000)),
        "max_tokens": max(256, int(m.get("max_tokens") or 4096)),
        "enabled": bool(m.get("enabled", True)),
        "added_at": m.get("added_at") or time.strftime("%Y-%m-%d"),
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
        over["providers"][pname] = {"type": m["format"], "base_url": m["base_url"], "api_key": m["api_key"]}
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
