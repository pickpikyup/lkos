"""在线模型配置：输出上限留空 / 旧文件迁移、高级参数透传、参数被拒绝时自动调整重试、配置文件编辑（JSON / YAML）、
文件写错时不覆盖、联网搜索词。"""

import json

import httpx
import pytest
import yaml

from lkos.gateway.base import ChatOptions, ChatResult
from lkos.gateway.online import AnthropicProvider, OpenAICompatProvider, online_provider
from lkos.models_config import (ModelsFileError, load_models, load_models_checked, models_path, parse_models,
                                save_models, to_overrides)
from lkos.web.search import clean_query, web_queries
from tests.conftest import TEST_DIM, fake_overrides


def sse(events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


OK = sse([{"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}])


def entry(**kw):
    return {"id": "ds", "preset": "deepseek", "name": "DeepSeek", "format": "openai", "base_url": "https://api.deepseek.com/v1",
            "api_key": "k", "model": "deepseek-flash", "context_window": 1000000, **kw}


# ---------- 配置 ----------
def test_legacy_default_output_limit_migrates_to_service_default(tmp_path):
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"online": [entry(max_tokens=4096)]}), "utf-8")          # 旧版本：4096 是程序默认值
    m = load_models(tmp_path)["online"][0]
    assert m["max_tokens"] is None
    assert m["extra_body_short"] == {"thinking": {"type": "disabled"}}                # DeepSeek：简短调用关闭思考
    over = to_overrides(load_models(tmp_path))
    assert over["models"]["chat"]["on-ds/deepseek-flash"]["max_output"] is None
    assert over["providers"]["on-ds"]["extra_body_short"] == {"thinking": {"type": "disabled"}}
    p.write_text(json.dumps({"version": 2, "online": [entry(max_tokens=4096, extra_body_short={})]}), "utf-8")
    m = load_models(tmp_path)["online"][0]
    assert m["max_tokens"] == 4096 and m["extra_body_short"] == {}                    # 新版本里明确写的值照用


def test_advanced_keys_checked_and_unknown_keys_kept(tmp_path):
    mc = parse_models({"version": 2, "online": [entry(temperature=None, extra_body={"reasoning_effort": "low"},
                                                      headers={"X-Org": "a"}, my_note="自己的备注")]})
    m = mc["online"][0]
    assert m["temperature"] is None and m["extra_body"] == {"reasoning_effort": "low"} and m["my_note"] == "自己的备注"
    save_models(tmp_path, mc)
    assert json.loads(models_path(tmp_path).read_text("utf-8"))["version"] == 2
    assert load_models(tmp_path)["online"][0]["my_note"] == "自己的备注"
    for bad, word in ((entry(extra_body=[1]), "extra_body"), (entry(temperature="hot"), "temperature"),
                      (entry(max_tokens="很多"), "max_tokens"), (entry(max_tokens_param="tokens"), "max_tokens_param"),
                      (entry(headers={"a": 1}), "headers")):
        with pytest.raises(ModelsFileError, match=word):
            parse_models({"online": [bad]})
    with pytest.raises(ModelsFileError, match="重复"):
        parse_models({"online": [entry(), entry()]})
    with pytest.raises(ModelsFileError, match="列表"):
        parse_models({"online": {"a": 1}})


def test_broken_file_reports_line_and_is_never_overwritten(tmp_path):
    p = tmp_path / "models.json"
    p.write_text('{"online": [\n  {"id": "ds",}\n]}', "utf-8")
    mc, err = load_models_checked(tmp_path)
    assert mc is None and "第 2 行" in err


# ---------- provider 参数 ----------
def recorder(responses):
    seen = []

    def handle(req):
        seen.append({"body": json.loads(req.content), "headers": req.headers})
        r = responses.pop(0) if responses else OK
        return r if isinstance(r, httpx.Response) else httpx.Response(200, content=r)
    return seen, httpx.MockTransport(handle)


def test_openai_params_service_default_extra_body_and_headers():
    seen, t = recorder([])
    p = online_provider("on-ds", "openai", {"base_url": "https://api.deepseek.com/v1", "api_key": "k", "temperature": None,
                                            "extra_body": {"reasoning_effort": "low"},
                                            "extra_body_short": {"thinking": {"type": "disabled"}},
                                            "headers": {"X-Org": "a"}}, transport=t)
    p.chat("m", [{"role": "user", "content": "x"}], ChatOptions(max_tokens=None))
    b = seen[-1]["body"]
    assert "max_tokens" not in b and "temperature" not in b and b["reasoning_effort"] == "low" and "thinking" not in b
    assert seen[-1]["headers"]["x-org"] == "a"
    p.chat("m", [{"role": "user", "content": "x"}], ChatOptions(max_tokens=500, short=True))
    b = seen[-1]["body"]
    assert b["max_tokens"] == 500 and b["thinking"] == {"type": "disabled"}
    q = online_provider("x", "openai", {"base_url": "https://a/v1", "api_key": "k", "max_tokens_param": "max_completion_tokens"},
                        transport=t)
    q.chat("m", [], ChatOptions(max_tokens=300, temperature=0.2))
    assert seen[-1]["body"]["max_completion_tokens"] == 300 and seen[-1]["body"]["temperature"] == 0.2


def test_rejected_params_are_adjusted_and_remembered():
    bad = lambda msg: httpx.Response(400, json={"error": {"message": msg}})       # noqa: E731
    seen, t = recorder([bad("Unsupported value: 'temperature' does not support 0.3 with this model."),
                        bad("Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.")])
    p = OpenAICompatProvider("o", "https://api.openai.com/v1", "k", transport=t)
    r = p.chat("o-model", [{"role": "user", "content": "x"}], ChatOptions(max_tokens=1000))
    assert r.text == "好" and len(seen) == 3
    assert "temperature" not in seen[-1]["body"] and seen[-1]["body"]["max_completion_tokens"] == 1000
    p.chat("o-model", [], ChatOptions(max_tokens=50))                                # 记住了：不再试错
    assert len(seen) == 4 and "temperature" not in seen[-1]["body"] and seen[-1]["body"]["max_completion_tokens"] == 50
    # 超出上限：消息里有更小的数字就用它
    seen, t = recorder([bad("max_tokens is too large: 65536. This model supports at most 8192 completion tokens.")])
    p = OpenAICompatProvider("o", "https://x/v1", "k", transport=t)
    p.chat("m", [], ChatOptions(max_tokens=65536))
    assert seen[-1]["body"]["max_tokens"] == 8192
    # Claude：必须给 max_tokens，没配置时 16000；超出时按提示的上限
    seen, t = recorder([bad("max_tokens: 16000 > 8192, which is the maximum allowed number of output tokens for claude-x"),
                        sse([{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "好"}}])])
    a = AnthropicProvider("c", "https://api.anthropic.com", "k", transport=t)
    assert a.chat("claude-x", [{"role": "user", "content": "x"}], ChatOptions(max_tokens=None)).text == "好"
    assert seen[0]["body"]["max_tokens"] == 16000 and seen[1]["body"]["max_tokens"] == 8192
    # 简短调用附加的参数被拒绝：去掉后重试
    seen, t = recorder([bad("Unknown field: thinking")])
    p = OpenAICompatProvider("o", "https://x/v1", "k", transport=t, extra_body_short={"thinking": {"type": "disabled"}})
    p.chat("m", [], ChatOptions(max_tokens=None, short=True))
    assert "thinking" in seen[0]["body"] and "thinking" not in seen[1]["body"]
    # 没法调整的错误照常报
    seen, t = recorder([bad("messages: field required")])
    with pytest.raises(Exception, match="HTTP 400"):
        OpenAICompatProvider("o", "https://x/v1", "k", transport=t).chat("m", [], ChatOptions(max_tokens=None))


def test_gateway_answer_check_messages(tmp_path):
    from lkos.app import App
    from lkos.errors import ModelUnavailableError

    save_models(tmp_path, {"local": {"chat": "x"}, "online": [entry(), entry(id="lim", max_tokens=2000)]})
    a = App.open(home=tmp_path, overrides=fake_overrides(), providers={})
    try:
        empty = ChatResult("", 1, 1, None, 1, thinking="想" * 30, finish_reason="length")
        with pytest.raises(ModelUnavailableError, match="配置文件"):
            a.gateway.ensure_answer(empty, "on-ds/deepseek-flash")         # 服务商默认也被思考用完：指向配置文件
        with pytest.raises(ModelUnavailableError, match="编辑"):
            a.gateway.ensure_answer(empty, "on-lim/deepseek-flash")        # 设了上限：指向“编辑”里的输出上限
        a.gateway.ensure_answer(ChatResult("有内容", 1, 1, 1, 1, finish_reason="length"), "on-ds/deepseek-flash")
    finally:
        a.close()


# ---------- 界面：编辑配置文件 ----------
@pytest.fixture
def bridge(tmp_path, corpus):
    from lkos.ui.bridge import Bridge

    home = tmp_path / "h"
    save_models(home, {"local": {"chat": "x", "context_window": 4096}, "embedding": {"model": "bge-m3", "dim": 1024},
                       "online": [entry(api_key="sk-secret", my_note="保留我")]})
    b = Bridge(home=home, overrides=fake_overrides(), providers={"test": __import__("lkos.gateway.fake", fromlist=["x"]).FakeProvider("test", dim=TEST_DIM)},
               auto_refresh=False)
    yield b
    b._close()


def test_models_file_edit_yaml_json_and_broken_file(bridge):
    home = bridge._app.home
    f = bridge.models_file("yaml")
    data = yaml.safe_load(f["text"])
    assert f["error"] is None and data["online"][0]["api_key"] == "sk-secret" and data["online"][0]["max_tokens"] is None
    assert "extra_body" in f["help"]
    # 表单保存：界面上没有的字段（高级参数、自己的备注）都保留
    r = bridge.save_online({"id": "ds", "preset": "deepseek", "name": "DS2", "format": "openai",
                            "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash", "max_tokens": 32768})
    assert r["ok"]
    m = load_models(home)["online"][0]
    assert m["name"] == "DS2" and m["max_tokens"] == 32768 and m["my_note"] == "保留我" and m["api_key"] == "sk-secret"
    assert m["extra_body_short"] == {"thinking": {"type": "disabled"}}
    # 用 YAML 改：加一个参数，保存后写成 JSON 并生效
    data = yaml.safe_load(bridge.models_file("yaml")["text"])
    data["online"][0]["temperature"] = None
    data["online"][0]["extra_body"] = {"reasoning_effort": "low"}
    text = yaml.safe_dump(data, allow_unicode=True)
    assert bridge.check_models_text(text, "yaml")["ok"]
    r = bridge.save_models_text(text, "yaml")
    assert r["ok"] and r["file_error"] is None
    saved = json.loads(models_path(home).read_text("utf-8"))
    assert saved["online"][0]["extra_body"] == {"reasoning_effort": "low"} and saved["online"][0]["temperature"] is None
    prov = bridge._app.gateway.provider("on-ds")
    assert prov.temperature is None and prov.extra_body == {"reasoning_effort": "low"}
    # 写错的内容：不保存，原文件不变
    before = models_path(home).read_text("utf-8")
    r = bridge.save_models_text('{"online": [ {"id": "ds", } ]}', "json")
    assert "第 1 行" in r["error"] and models_path(home).read_text("utf-8") == before
    r = bridge.save_models_text("online:\n  - id: ds\n    extra_body: 3\n", "yaml")
    assert "extra_body" in r["error"] and models_path(home).read_text("utf-8") == before
    # 在别的编辑器里改坏了文件：提示错误；界面上的保存一律拒绝，不会用默认值覆盖（连同密钥）
    models_path(home).write_text('{"online": [', "utf-8")
    info = bridge.models()
    assert info["file_error"] and "JSON" in info["file_error"]
    assert "配置文件有错误" in bridge.save_online(entry(id="new"))["error"]
    assert "配置文件有错误" in bridge.set_offline(True)["error"]
    assert "配置文件有错误" in bridge.consent_online("ds")["error"]
    assert models_path(home).read_text("utf-8") == '{"online": ['
    assert bridge.reload_models_file()["error"]
    f = bridge.models_file("json")
    assert f["error"] and f["text"] == '{"online": ['                       # 编辑器里显示原文，就地修改
    models_path(home).write_text(before, "utf-8")
    assert bridge.reload_models_file()["ok"]


# ---------- 联网搜索词 ----------
def test_web_search_terms_drop_instructions_and_duplicates():
    q = "现在本地查找 Ascend 960 关键参数，然后结合互联网信息核实"
    kw = {"zh": "Ascend 960 关键参数 互联网核实", "en": "Ascend 960 key parameters internet verification"}
    aspects = ["Ascend 960 官方规格与关键参数有哪些？", "算力和功耗是多少？"]
    assert clean_query(q) == "Ascend 960 关键参数"
    assert web_queries(q, kw, aspects, "zh", True) == ["Ascend 960 关键参数", "Ascend 960 key parameters",
                                                       "Ascend 960 官方规格与关键参数", "Ascend 960 算力和功耗"]
    assert web_queries(q, kw, aspects, "zh", False) == ["Ascend 960 关键参数"]
    assert web_queries(q, {}, [], "zh", True) == ["Ascend 960 关键参数"]


# ---------- 回归：2026-11-22 深度分析时 DeepSeek 的思考把 4096 的输出上限用光 ----------
def test_deep_analysis_with_deepseek_thinking_end_to_end(tmp_path, corpus):
    """旧版 models.json（max_tokens 4096、没有 version）+ 默认开思考的 deepseek-flash：
    拆方面 / 关键词关闭思考、不传输出上限；最后的分析不传输出上限（服务商默认），思考再长也能写出回答。"""
    from lkos.app import App

    home = tmp_path / "h"
    home.mkdir()
    (home / "models.json").write_text(json.dumps({
        "local": {"chat": "x", "context_window": 4096}, "embedding": {"model": "bge-m3", "dim": 1024},
        "online": [entry(id="deepseek", max_tokens=4096)]}), "utf-8")
    seen = []

    def handle(req):
        body = json.loads(req.content)
        seen.append(body)
        user = body["messages"][-1]["content"]
        thinking_on = body.get("thinking", {}).get("type") != "disabled"
        limit = body.get("max_tokens", 65536)
        think = "想" * 9731 if thinking_on else ""
        if len(think) // 2 >= limit:                        # 思考就把上限用光：只想不写
            return httpx.Response(200, content=sse([{"choices": [{"delta": {"reasoning_content": think}, "finish_reason": "length"}]}]))
        if "拆成 2–4 个" in user:
            text = "1. 预算\n2. 进度"
        elif "检索关键词" in user:
            text = "中文：预算\nEnglish: budget\nFrançais: budget"
        else:
            text = "### 预算\n一百二十万 [1]\n### 结论\n可控 [1]"
        events = ([{"choices": [{"delta": {"reasoning_content": think}}]}] if think else []) + \
                 [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}]
        return httpx.Response(200, content=sse(events))

    from lkos.gateway.fake import FakeProvider
    a = App.open(home=home, overrides=fake_overrides(), providers={"test": FakeProvider("test", dim=TEST_DIM)})
    try:
        a.cfg.providers["local"] = a.cfg.providers["test"]
        a.indexer.index_folder(corpus)
        pc = a.cfg.providers["on-deepseek"]
        a.gateway._providers["on-deepseek"] = online_provider("on-deepseek", pc.type, pc.options,
                                                              transport=httpx.MockTransport(handle))
        thoughts = []
        ans, aspects, _ = a.analyzer.analyze("项目二期的预算和进度怎么样？", model_override="on-deepseek/deepseek-flash",
                                             on_thinking=thoughts.append)
    finally:
        a.close()
    plan, kw, final = seen
    assert aspects == ["预算", "进度"] and "一百二十万" in ans.text and ans.citations
    for b in (plan, kw):
        assert b["thinking"] == {"type": "disabled"} and "max_tokens" not in b       # 简短调用：关思考、不截断
    assert "thinking" not in final and "max_tokens" not in final                      # 最后的分析：思考照开，按服务商默认上限
    assert len("".join(thoughts)) == 9731                                             # 思考过程推给了界面
