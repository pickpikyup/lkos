import json

import httpx
import pytest

from lkos.errors import ModelUnavailableError
from lkos.gateway.base import ChatOptions
from lkos.gateway.ollama import OllamaProvider


def provider(handler) -> OllamaProvider:
    return OllamaProvider("local", transport=httpx.MockTransport(handler))


def ndjson(*objs) -> bytes:
    return ("\n".join(json.dumps(o) for o in objs) + "\n").encode()


def test_chat_streams_tokens_and_sends_required_options():
    seen = {}

    def handler(req: httpx.Request):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=ndjson(
            {"message": {"content": "二期"}, "done": False},
            {"message": {"content": ""}, "done": False},
            {"message": {"content": "预算 [1]"}, "done": False},
            {"done": True, "prompt_eval_count": 120, "eval_count": 7},
        ))

    tokens: list[str] = []
    res = provider(handler).chat("qwen3:4b", [{"role": "user", "content": "q"}],
                                 ChatOptions(temperature=0.2, max_tokens=256, num_ctx=8192, think=False),
                                 on_token=tokens.append)
    assert res.text == "二期预算 [1]"
    assert tokens == ["二期", "预算 [1]"]
    assert (res.input_tokens, res.output_tokens) == (120, 7)
    assert res.ttft_ms is not None and res.latency_ms >= res.ttft_ms
    body = seen["body"]
    assert seen["path"] == "/api/chat"
    assert body["stream"] is True and body["think"] is False and body["keep_alive"] == "30m"
    assert body["options"] == {"temperature": 0.2, "num_ctx": 8192, "num_predict": 256}


def test_chat_error_in_stream():
    def handler(req):
        return httpx.Response(200, content=ndjson({"error": "out of memory"}))

    with pytest.raises(ModelUnavailableError, match="out of memory"):
        provider(handler).chat("m", [], ChatOptions())


def test_chat_404_suggests_pull():
    def handler(req):
        return httpx.Response(404, json={"error": "model 'qwen3:4b' not found"})

    with pytest.raises(ModelUnavailableError, match="ollama pull qwen3:4b"):
        provider(handler).chat("qwen3:4b", [], ChatOptions())


def test_connection_error_explains_how_to_start():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ModelUnavailableError, match="无法连接 Ollama"):
        provider(handler).chat("m", [], ChatOptions())
    with pytest.raises(ModelUnavailableError, match="无法连接 Ollama"):
        provider(handler).embed("m", ["x"])


def test_timeout_is_reported():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ModelUnavailableError, match="超时"):
        provider(handler).embed("m", ["x"])


def test_embed_batch():
    def handler(req):
        body = json.loads(req.content)
        assert req.url.path == "/api/embed" and body["input"] == ["a", "b"]
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    assert provider(handler).embed("bge-m3", ["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_empty_input_makes_no_request():
    def handler(req):  # pragma: no cover - 不应被调用
        raise AssertionError("不应发出请求")

    assert provider(handler).embed("m", []) == []


def test_embed_malformed_response():
    def handler(req):
        return httpx.Response(200, json={"embeddings": [[0.1]]})

    with pytest.raises(ModelUnavailableError, match="格式异常"):
        provider(handler).embed("m", ["a", "b"])


def test_check_model_via_tags():
    def handler(req):
        assert req.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen3:4b"}, {"name": "bge-m3:latest"}]})

    p = provider(handler)
    assert p.check_model("qwen3:4b").ok
    assert p.check_model("bge-m3").ok  # 不带标签时按 :latest 匹配
    miss = p.check_model("qwen3:8b")
    assert not miss.ok and "ollama pull qwen3:8b" in miss.message


def test_check_model_when_ollama_down():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    res = provider(handler).check_model("qwen3:4b")
    assert not res.ok and "无法连接" in res.message


def test_chat_retries_without_think_when_model_rejects_it():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if "think" in body:
            return httpx.Response(400, json={"error": '"qwen3:4b-instruct" does not support thinking'})
        return httpx.Response(200, content=ndjson({"message": {"content": "2"}, "done": False},
                                                   {"done": True, "load_duration": 2_500_000_000}))

    res = provider(handler).chat("qwen3:4b-instruct", [], ChatOptions(think=False))
    assert res.text == "2" and res.load_ms == 2500
    assert "think" in bodies[0] and "think" not in bodies[1]


def test_other_400_errors_are_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": "invalid options"})

    with pytest.raises(ModelUnavailableError, match="invalid options"):
        provider(handler).chat("m", [], ChatOptions())
    assert len(calls) == 1
