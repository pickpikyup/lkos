"""推理模型的思考过程（不混进正文、额度用完时的处理）与联网搜索（解析、取正文、开关随时生效、作为出处）。"""

import base64
import json
import time

import httpx
import pytest

from lkos.errors import ModelUnavailableError
from lkos.gateway.base import ChatOptions, ChatResult
from lkos.gateway.fake import FakeProvider
from lkos.gateway.online import AnthropicProvider, OpenAICompatProvider
from lkos.models_config import save_models
from lkos.web.search import (WebSearcher, best_passages, domain, page_text, parse_bing, parse_duckduckgo,
                             query_terms)
from tests.conftest import TEST_DIM, fake_overrides

ONLINE = "on-cloud/big-model"


def sse(events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


# ---------- 在线模型：思考过程 ----------
def test_openai_reasoning_content_is_separate_from_answer():
    def handle(req):
        body = json.loads(req.content)
        if body["max_tokens"] < 50:       # 额度太小：只想不写
            return httpx.Response(200, content=sse([
                {"choices": [{"delta": {"reasoning_content": "要先想清楚"}}]},
                {"choices": [{"delta": {"reasoning_content": "……"}, "finish_reason": "length"}],
                 "usage": {"prompt_tokens": 9, "completion_tokens": 20}}]))
        return httpx.Response(200, content=sse([
            {"choices": [{"delta": {"role": "assistant", "reasoning_content": "用户问预算，"}}]},
            {"choices": [{"delta": {"reasoning_content": "资料 [1] 有。"}}]},
            {"choices": [{"delta": {"content": "预算一百二十万 [1]"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 30, "completion_tokens": 12}}]))

    p = OpenAICompatProvider("on-d", "https://api.deepseek.com/v1", "k", transport=httpx.MockTransport(handle))
    tokens, thoughts = [], []
    r = p.chat("deepseek-flash", [{"role": "user", "content": "预算？"}], ChatOptions(max_tokens=500), tokens.append,
               on_thinking=thoughts.append)
    assert r.text == "".join(tokens) == "预算一百二十万 [1]"           # 思考不混进回答
    assert r.thinking == "".join(thoughts) == "用户问预算，资料 [1] 有。"
    assert r.finish_reason == "stop" and not r.out_of_budget and r.ttft_ms is not None
    r = p.chat("deepseek-flash", [{"role": "user", "content": "预算？"}], ChatOptions(max_tokens=20))
    assert r.text == "" and r.out_of_budget and r.thinking == "要先想清楚……" and r.ttft_ms is None


def test_anthropic_thinking_delta_and_stop_reason():
    def handle(req):
        return httpx.Response(200, content=sse([
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "想一想"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "好"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 3}}]))

    p = AnthropicProvider("on-c", "https://api.anthropic.com", "k", transport=httpx.MockTransport(handle))
    seen = []
    r = p.chat("claude", [{"role": "user", "content": "x"}], ChatOptions(), on_thinking=seen.append)
    assert r.text == "好" and r.thinking == "想一想" and seen == ["想一想"] and r.finish_reason == "length"
    assert not r.out_of_budget                                           # 写出了正文就不算


def test_gateway_passes_thinking_only_to_providers_that_accept_it(cfg):
    from lkos.gateway.gateway import ModelGateway, out_of_budget_message

    class Old:          # 旧接口：没有 on_thinking 参数
        is_remote = False

        def chat(self, model, messages, opts, on_token=None):
            return ChatResult("ok", 1, 1, 1, 1)

    class New(Old):
        def chat(self, model, messages, opts, on_token=None, on_thinking=None):
            on_thinking("嗯")
            return ChatResult("", 1, 1, None, 1, thinking="嗯", finish_reason="length")

    seen = []
    r, _ = ModelGateway(cfg, providers={"test": Old()}).chat("knowledge_qa", [], on_thinking=seen.append)
    assert r.text == "ok" and seen == []
    r, _ = ModelGateway(cfg, providers={"test": New()}).chat("knowledge_qa", [], on_thinking=seen.append)
    assert seen == ["嗯"] and r.out_of_budget
    assert "思考" in out_of_budget_message(r, 160) and "160" in out_of_budget_message(r, 160)


# ---------- 联网搜索：解析 ----------
def bing_link(url):
    return "https://www.bing.com/ck/a?!&amp;&amp;p=abc&amp;u=a1" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


BING = f"""<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="{bing_link('https://example.com/a')}" h="ID=1">Ooredoo 2025 <strong>年报</strong></a></h2>
<div class="b_caption"><p class="b_lineclamp2">Ooredoo 集团 2025 年收入增长 &amp; 战略更新</p></div></li>
<li class="b_algo"><h2><a href="https://news.example.org/b">运营商 AI 案例</a></h2><div class="b_caption"><p>某运营商与合作伙伴发布 AI 服务</p></div></li>
<li class="b_ad"><h2><a href="https://ads.example/x">广告</a></h2></li>
</ol></body></html>"""

DDG = """<div class="result results_links web-result"><div class="links_main">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">Ooredoo report</a>
<a class="result__snippet" href="#">Revenue grew in <b>2025</b></a></div></div>
<div class="result result--ad"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>"""

PAGE_A = """<html><head><title>Ooredoo 2025 年报</title><script>var x = "收入";</script></head><body>
<nav>首页 关于我们 联系我们</nav>
<p>Ooredoo 集团 2025 年全年收入为 233 亿卡塔尔里亚尔，同比增长 3%，其中阿尔及利亚市场收入增长明显，数字业务占比继续提高。</p>
<p>集团在 2025 年更新了战略，重点投入数据中心、人工智能和海底光缆，计划未来三年把数字基础设施收入翻一番。</p>
<footer>版权所有 © 2025</footer></body></html>"""

PAGE_B = "<html><title>案例</title><body><div>某运营商在 2026 年与合作伙伴发布面向企业的人工智能服务，覆盖客服、网络运维和营销三类场景，首批签约客户数百家。</div></body></html>"


def web_handler(seen):
    def handle(req: httpx.Request):
        seen.append(str(req.url))
        host = req.url.host
        if host == "cn.bing.com":
            return httpx.Response(200, text=BING, headers={"content-type": "text/html; charset=utf-8"})
        if host == "html.duckduckgo.com":
            return httpx.Response(200, text=DDG, headers={"content-type": "text/html"})
        if host == "api.tavily.com":
            if req.headers.get("authorization") != "Bearer tvly-good":
                return httpx.Response(401, json={"detail": "bad key"})
            return httpx.Response(200, json={"results": [
                {"title": "T1", "url": "https://t.example/1", "content": "Ooredoo 2025 收入增长，战略聚焦人工智能。"}]})
        if host == "example.com":
            return httpx.Response(200, text=PAGE_A, headers={"content-type": "text/html; charset=utf-8"})
        if host == "news.example.org":
            return httpx.Response(200, text=PAGE_B, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404)
    return handle


def test_parse_results_and_page_text():
    res = parse_bing(BING, 5)
    assert [r.url for r in res] == ["https://example.com/a", "https://news.example.org/b"]    # 还原必应的跳转链接、跳过广告
    assert res[0].title == "Ooredoo 2025 年报" and "收入增长 & 战略" in res[0].snippet
    res = parse_duckduckgo(DDG, 5)
    assert [r.url for r in res] == ["https://example.com/a"] and res[0].snippet == "Revenue grew in 2025"
    title, paras = page_text(PAGE_A)
    assert title == "Ooredoo 2025 年报" and len(paras) == 2 and not any("首页" in p or "var x" in p for p in paras)
    assert "ooredoo" in query_terms("Ooredoo 2025 战略更新") and "战略" in query_terms("Ooredoo 2025 战略更新")
    best = best_passages(paras, "战略 人工智能", 60)
    assert best.startswith("集团在 2025 年更新了战略")                       # 先取最相关的段落
    assert domain("https://www.example.com/x") == "example.com"


def test_web_searcher_providers_switch_and_failures():
    seen, settings = [], {"web_search": False}
    offline = {"on": False}
    w = WebSearcher(settings=lambda: settings, offline=lambda: offline["on"], transport=httpx.MockTransport(web_handler(seen)))
    w.begin()
    assert w.chunks("Ooredoo 战略") == [] and not seen and w.run.skipped == "联网搜索已关闭"
    settings["web_search"] = True                                       # 随时打开，下一次就生效
    chunks = w.chunks("Ooredoo 2025 战略", k=5, fetch=2, max_chars=500)
    assert [c.file_path for c in chunks] == ["https://example.com/a", "https://news.example.org/b"]
    c = chunks[0]
    assert c.file_type == "web" and c.chunk_id < 0 and c.file_name == "example.com"
    assert c.describe() == "网页 · example.com · Ooredoo 2025 年报" and "战略" in c.text
    assert w.run.queries == ["Ooredoo 2025 战略"] and w.run.pages == 2
    settings.update(web_provider="tavily", web_key="")
    assert w.chunks("x") == [] and "密钥" in w.run.errors[-1]
    settings["web_key"] = "tvly-good"
    assert [c.file_path for c in w.chunks("Ooredoo")] == ["https://t.example/1"]
    settings["web_key"] = "tvly-bad"
    assert w.chunks("Ooredoo") == [] and "无效" in w.run.errors[-1]
    settings["web_provider"] = "duckduckgo"
    assert [c.file_path for c in w.chunks("Ooredoo report")] == ["https://example.com/a"]
    offline["on"] = True
    assert w.chunks("Ooredoo") == [] and "离线" in w.run.skipped
    assert w.run.to_dict()["queries"] and w.run.to_dict()["errors"]


# ---------- 接入问答 / 对话 / 深度分析 ----------
class Thinker(FakeProvider):
    """带思考过程的模型：记下每次的输出上限；可以让拆方面或最后的分析把额度都用在思考上。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.limits, self.messages = [], []
        self.plan_budget = self.final_budget = False

    def chat(self, model, messages, opts, on_token=None, on_thinking=None):
        self.limits.append(opts.max_tokens)
        self.messages.append(messages)
        user = messages[-1]["content"]
        if on_thinking:
            on_thinking("先想一想。")
        plan = "拆成 2–4 个" in user
        if (plan and self.plan_budget) or (self.final_budget and "请按这几个方面分析" in user):
            return ChatResult("", 10, opts.max_tokens, None, 1, thinking="想了很久" * 5, finish_reason="length")
        if plan:
            self.answer_fn = lambda m: "1. 预算\n2. 进度"
        elif "检索关键词" in user:
            self.answer_fn = lambda m: "中文：预算\nEnglish: budget\nFrançais: budget"
        else:
            self.answer_fn = None
        r = super().chat(model, messages, opts, on_token)
        r.thinking, r.finish_reason = "先想一想。", "stop"
        return r


@pytest.fixture
def setup(tmp_path, corpus):
    from lkos.ui.bridge import Bridge

    home = tmp_path / "h"
    save_models(home, {"local": {"chat": "x", "context_window": 8192}, "embedding": {"model": "bge-m3", "dim": 1024},
                       "online": [{"id": "cloud", "name": "云端大模型", "format": "openai", "base_url": "https://cloud.example/v1",
                                   "api_key": "k", "model": "big-model", "context_window": 100000}],
                       "offline_mode": False})
    local, cloud = Thinker("test", dim=TEST_DIM), Thinker("on-cloud", dim=TEST_DIM)
    b = Bridge(home=home, overrides=fake_overrides(), providers={"test": local, "on-cloud": cloud}, auto_refresh=False)
    b._call(lambda: b._app.indexer.index_folder(corpus))
    seen = []
    b._app.web._transport = httpx.MockTransport(web_handler(seen))
    b.set_setting("confirm_online", False)
    b.consent_online("cloud")
    yield b, local, cloud, seen
    b._close()


def done(b, out, timeout=10, expect="done"):
    assert "qid" in out, out
    end = time.time() + timeout
    while time.time() < end:
        for e in b._sent:
            if e.get("qid") == out["qid"] and e["type"] in ("done", "error", "stopped"):
                assert e["type"] == expect, e
                return e.get("record") or e
        time.sleep(0.03)
    raise AssertionError("超时")


def test_online_analysis_short_calls_get_room_to_think(setup):
    b, local, cloud, _ = setup
    cloud.plan_budget = True
    details, thoughts = {}, []
    ans, aspects, info = b._call(lambda: b._app.analyzer.analyze(
        "项目二期的预算和进度怎么样？", model_override=ONLINE, on_thinking=thoughts.append,
        on_progress=lambda s, d, t, x=None: details.update(x or {})))
    assert cloud.limits[:2] == [2048, 2048]                         # 在线模型拆方面 / 写关键词：留足思考的额度
    assert aspects == ["项目二期的预算和进度怎么样？"] and "思考" in details["note"]   # 拆分失败时按整个问题检索，并说明原因
    assert info["keywords"] and ans.text and thoughts
    local.limits.clear()
    b._call(lambda: b._app.analyzer.analyze("项目二期的预算和进度怎么样？"))
    assert local.limits[:2] == [160, 120]                            # 本机模型不变
    cloud.plan_budget, cloud.final_budget = False, True
    with pytest.raises(ModelUnavailableError, match="思考"):
        b._call(lambda: b._app.analyzer.analyze("项目二期的预算和进度怎么样？", model_override=ONLINE))


def test_thinking_events_and_web_sources_in_every_mode(setup):
    b, local, cloud, seen = setup
    # 对话（在线）：思考过程单独推送；联网关着时没有出处
    out = b.send("你好", mode="chat", model=ONLINE)
    rec = done(b, out)
    assert [e["text"] for e in b._sent if e["type"] == "thinking" and e["qid"] == out["qid"]] == ["先想一想。"]
    assert rec["context"] == [] and not seen and "web" not in rec["extra"]
    # 打开联网：分析、对话都带上网页出处
    assert b.set_setting("web_search", True)["web_search"] is True
    rec = done(b, b.send("Ooredoo 2025 战略", mode="qa"))
    web = [c for c in rec["context"] if c["type"] == "web"]
    assert web and web[0]["path"].startswith("https://") and web[0]["file"] in ("example.com", "news.example.org")
    assert rec["extra"]["web"]["queries"] == ["Ooredoo 2025 战略"] and rec["extra"]["web"]["pages"] >= 1
    assert any("网页" in m["content"] for m in local.messages[-1] if m["role"] == "user")
    assert "联网搜索" in local.messages[-1][0]["content"]                  # 系统提示说明了网页资料
    rec = done(b, b.send("Ooredoo 2025 战略", mode="chat", model=ONLINE))
    assert rec["context"] and all(c["type"] == "web" for c in rec["context"]) and rec["cited"]
    # 深度分析进行中打开开关：还没做的检索马上联网
    b.set_setting("web_search", False)
    n = len(seen)

    def flip(stage, done_, total, detail=None):
        if stage == "keywords" and done_:
            b.set_setting("web_search", True)
    ans, _, _ = b._call(lambda: b._app.analyzer.analyze("Ooredoo 2025 战略", on_progress=flip))
    assert len(seen) > n and any(c.file_type == "web" for c in ans.context)
    # 搜索服务出错不影响回答
    b.set_setting("web_provider", "tavily")
    rec = done(b, b.send("项目二期的预算上限是多少？", mode="qa"))
    assert rec["answer"] and "密钥" in rec["extra"]["web"]["errors"][0]


def test_bridge_open_url_and_test_search(setup):
    b, *_ = setup
    assert b.open_url("file:///etc/passwd")["error"]
    assert b.test_web_search()["ok"]
    b._app.cfg.privacy.offline_mode = True
    assert not b.test_web_search()["ok"]
