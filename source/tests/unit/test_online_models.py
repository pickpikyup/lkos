"""在线模型：Anthropic / OpenAI 兼容接口的流式调用与错误；模型配置的保存、合并、更换向量模型。"""

import json

import httpx
import pytest

from lkos.app import App
from lkos.errors import Cancelled, ModelUnavailableError, RemoteDisabledError
from lkos.gateway.base import ChatOptions
from lkos.gateway.fake import FakeProvider
from lkos.gateway.online import AnthropicProvider, OpenAICompatProvider
from lkos.models_config import display_name, load_models, model_choices, save_models, to_overrides
from tests.conftest import TEST_DIM, fake_overrides


def sse(events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def anthropic_handler(seen):
    def handle(req: httpx.Request):
        seen.append(req)
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "claude-sonnet-5-5"}, {"id": "claude-haiku-5-5"}]})
        body = json.loads(req.content)
        if req.headers.get("x-api-key") != "good":
            return httpx.Response(401, json={"type": "error", "error": {"message": "invalid x-api-key"}})
        if body["model"] == "nope":
            return httpx.Response(404, json={"error": {"message": "model: nope"}})
        return httpx.Response(200, content=sse([
            {"type": "message_start", "message": {"usage": {"input_tokens": 42}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "二期预算"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "一百二十万 [1]"}},
            {"type": "message_delta", "usage": {"output_tokens": 9}},
            {"type": "message_stop"}]), headers={"content-type": "text/event-stream"})
    return handle


def test_anthropic_stream_and_errors():
    seen = []
    p = AnthropicProvider("on-c", "https://api.anthropic.com", "good", transport=httpx.MockTransport(anthropic_handler(seen)))
    tokens = []
    r = p.chat("claude-sonnet-5-5", [{"role": "system", "content": "只根据资料回答"},
                                     {"role": "user", "content": "预算？"}], ChatOptions(max_tokens=300), tokens.append)
    assert r.text == "二期预算一百二十万 [1]" and r.input_tokens == 42 and r.output_tokens == 9 and r.ttft_ms is not None
    assert "".join(tokens) == r.text
    req = seen[-1]
    body = json.loads(req.content)
    assert str(req.url) == "https://api.anthropic.com/v1/messages" and req.headers["anthropic-version"]
    assert body["system"] == "只根据资料回答" and body["messages"] == [{"role": "user", "content": "预算？"}]
    assert body["max_tokens"] == 300 and body["stream"] is True
    assert p.list_models() == ["claude-sonnet-5-5", "claude-haiku-5-5"]
    with pytest.raises(ModelUnavailableError, match="找不到模型"):
        p.chat("nope", [{"role": "user", "content": "x"}], ChatOptions())
    bad = AnthropicProvider("on-c", "https://api.anthropic.com/v1", "bad", transport=httpx.MockTransport(anthropic_handler(seen)))
    with pytest.raises(ModelUnavailableError, match="密钥无效"):
        bad.chat("claude-sonnet-5-5", [{"role": "user", "content": "x"}], ChatOptions())
    assert str(seen[-1].url) == "https://api.anthropic.com/v1/messages"         # 地址带 /v1 也行
    assert not bad.check_model("claude-sonnet-5-5").ok
    with pytest.raises(ModelUnavailableError, match="密钥"):
        AnthropicProvider("x", "https://api.anthropic.com", "").chat("m", [], ChatOptions())

    def stop(_):
        raise Cancelled("已停止生成")
    with pytest.raises(Cancelled):
        p.chat("claude-sonnet-5-5", [{"role": "user", "content": "x"}], ChatOptions(), stop)


def test_openai_compatible_stream():
    seen = []

    def handle(req):
        seen.append(req)
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-reasoner"}, {"id": "deepseek-chat"}]})
        if req.headers["authorization"] != "Bearer k":
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, content=sse([
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
        ]) + b"data: [DONE]\n\n")

    p = OpenAICompatProvider("on-d", "https://api.deepseek.com/v1/", "k", transport=httpx.MockTransport(handle))
    r = p.chat("deepseek-chat", [{"role": "user", "content": "hi"}], ChatOptions(max_tokens=50))
    assert r.text == "Hello world" and (r.input_tokens, r.output_tokens) == (7, 2)
    assert str(seen[-1].url) == "https://api.deepseek.com/v1/chat/completions"
    assert json.loads(seen[-1].content)["messages"] == [{"role": "user", "content": "hi"}]
    assert p.list_models() == ["deepseek-chat", "deepseek-reasoner"]
    q = OpenAICompatProvider("on-d", "https://api.deepseek.com/v1", "x", transport=httpx.MockTransport(handle))
    with pytest.raises(ModelUnavailableError, match="额度不足"):
        q.chat("deepseek-chat", [{"role": "user", "content": "hi"}], ChatOptions())


def test_models_json_overrides_and_choices(tmp_path):
    home = tmp_path / "h"
    mc = {"local": {"chat": "qwen3:8b", "context_window": 8192}, "embedding": {"model": "bge-m3", "dim": 1024},
          "online": [{"id": "claude", "name": "Claude Sonnet", "format": "anthropic", "base_url": "https://api.anthropic.com",
                      "api_key": "good", "model": "claude-sonnet-5-5", "context_window": 200000},
                     {"id": "off", "name": "停用的", "format": "openai", "base_url": "https://x", "api_key": "k",
                      "model": "m", "enabled": False}],
          "offline_mode": False}
    save_models(home, mc)
    loaded = load_models(home)
    over = to_overrides(loaded)
    assert over["tasks"]["knowledge_qa"]["default_model"] == "local/qwen3:8b"
    assert over["providers"]["on-claude"] == {"type": "anthropic", "base_url": "https://api.anthropic.com", "api_key": "good"}
    assert "on-off" not in over["providers"] and over["privacy"] == {"remote_enabled": True, "offline_mode": False}
    assert over["models"]["chat"]["on-claude/claude-sonnet-5-5"]["max_output"] == 4096
    assert (home / "models.json").stat().st_mode & 0o077 == 0
    # App 读取 models.json（测试覆盖仍优先）
    a = App.open(home=home, overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        assert a.cfg.providers["on-claude"].is_remote and a.cfg.privacy.remote_enabled
        assert a.cfg.chat_model("on-claude/claude-sonnet-5-5").context_window == 200000
        choices = model_choices(loaded, a.cfg)
        assert [c["ref"] for c in choices] == ["test/fake-chat", "on-claude/claude-sonnet-5-5"]
        assert choices[1]["online"] and not choices[1]["consented"]
        assert display_name("on-claude/claude-sonnet-5-5", loaded) == "Claude Sonnet"
        assert display_name("local/qwen3:8b", loaded) == "qwen3:8b"
    finally:
        a.close()
    loaded["offline_mode"] = True
    save_models(home, loaded)
    a = App.open(home=home, overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        with pytest.raises(RemoteDisabledError, match="离线模式"):
            a.gateway.chat("knowledge_qa", [{"role": "user", "content": "x"}], model_override="on-claude/claude-sonnet-5-5")
        assert len(model_choices(load_models(home), a.cfg)) == 1
    finally:
        a.close()


def test_bridge_model_config_and_embedding_switch(tmp_path, corpus, monkeypatch):
    from lkos.ui.bridge import Bridge

    calls = []

    class Fake(FakeProvider):
        def list_models(self):
            return ["qwen3:4b-instruct", "bge-m3", "nomic-embed-text"]

        def embed(self, model, texts):
            calls.append(model)
            return super().embed(model, texts) if model != "nomic-embed-text" else [[0.1] * 768 for _ in texts]

    over = fake_overrides(providers={"local": {"type": "fake"}})
    del over["models"]["embedding"]                     # 向量模型用默认的 local/bge-m3，之后由 models.json 更换
    b = Bridge(home=tmp_path / "h", overrides=over, providers={"test": Fake("test", dim=TEST_DIM),
                                                               "local": Fake("local", dim=1024)}, auto_refresh=False)
    try:
        b._call(lambda: b._app.indexer.index_folder(corpus))
        m = b.models()
        assert m["local"]["ok"] and "bge-m3" in m["local"]["models"]
        assert {p["id"] for p in m["presets"]} >= {"anthropic", "openai", "deepseek", "qwen", "kimi", "zhipu", "gemini",
                                                    "openrouter", "nous", "together", "groq", "siliconflow"}
        nous = next(p for p in m["presets"] if p["id"] == "nous")
        assert nous["group"] == "aggregator" and nous["base_url"] == "https://inference-api.nousresearch.com/v1"
        assert set(m["groups"]) == {"vendor", "aggregator"} and all(p["group"] in m["groups"] for p in m["presets"])
        assert len({p["id"] for p in m["presets"]}) == len(m["presets"])
        assert m["guide"]["base_url"]["example"] and m["config"]["online"] == []
        assert b.save_online({"name": "Claude", "format": "anthropic", "base_url": "https://api.anthropic.com",
                              "model": "claude-sonnet-5-5"})["error"] == "请填写API 密钥"
        r = b.save_online({"name": "Claude", "preset": "anthropic", "format": "anthropic",
                           "base_url": "https://api.anthropic.com", "api_key": "sk-ant-secret-1234",
                           "model": "claude-sonnet-5-5", "context_window": 200000})
        mid = r["id"]
        online = r["config"]["online"][0]
        assert "api_key" not in online and online["key_set"] and online["key_tail"] == "1234"
        assert [c["name"] for c in b.model_choices()] == ["fake-chat", "Claude"]
        r = b.save_online({"id": mid, "name": "Claude Sonnet", "format": "anthropic", "base_url": "https://api.anthropic.com",
                           "api_key": "", "model": "claude-sonnet-5-5"})          # 不填密钥：沿用
        assert load_models(tmp_path / "h")["online"][0]["api_key"] == "sk-ant-secret-1234"
        assert b._app.cfg.providers[f"on-{mid}"].options["api_key"] == "sk-ant-secret-1234"   # 已重新加载
        assert b.consent_online(mid)["ok"] and b.model_choices()[1]["consented"]
        assert len(b.set_offline(True)["choices"]) == 1 and len(b.set_offline(False)["choices"]) == 2
        assert b.delete_online(mid)["config"]["online"] == []
        # 更换向量模型
        assert b.probe_embedding("")["error"]
        p = b.probe_embedding("nomic-embed-text")
        assert p["dim"] == 768 and len(p["sources"]) == 1
        sid = p["sources"][0]["id"]
        r = b.apply_embedding("nomic-embed-text", 768, [sid], when="now")
        assert r["ok"] and r["folders"] == 1
        assert load_models(tmp_path / "h")["embedding"] == {"model": "nomic-embed-text", "dim": 768}
        meta = b._call(lambda: (b._app.repo.get_meta("embedding_model"), b._app.repo.get_meta("embedding_dim")))
        assert meta == ("local/nomic-embed-text", "768")
        assert b._app.cfg.embedding.ref == "local/nomic-embed-text" and b._app.cfg.embedding.dim == 768
        assert b.apply_embedding("nomic-embed-text", 768, [sid])["error"]        # 一样的不用换
    finally:
        b._close()
