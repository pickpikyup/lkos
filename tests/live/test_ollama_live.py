"""真实模型测试：需要本机 Ollama 已启动，且已下载 qwen3:4b-instruct 与 bge-m3。

运行：LKOS_LIVE=1 python -m pytest tests/live -v
"""

from __future__ import annotations

import os

import pytest

from lkos.app import App
from lkos.config import load_config
from lkos.gateway.base import ChatOptions
from lkos.gateway.ollama import OllamaProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("LKOS_LIVE") != "1", reason="设置 LKOS_LIVE=1 才运行真实模型测试"),
]


@pytest.fixture(scope="module")
def provider():
    cfg = load_config(None)
    return OllamaProvider("local", base_url=cfg.providers["local"].options["base_url"])


def test_models_downloaded(provider):
    assert provider.check_model("qwen3:4b-instruct").ok, "请执行 ollama pull qwen3:4b-instruct"
    assert provider.check_model("bge-m3").ok, "请执行 ollama pull bge-m3"


def test_embedding_dim_matches_config(provider):
    vecs = provider.embed("bge-m3", ["二期预算上限", "budget cap", "plafond budgétaire"])
    assert len(vecs) == 3 and all(len(v) == load_config(None).embedding.dim for v in vecs)


def test_chat_without_thinking_is_fast_and_follows_language(provider):
    res = provider.chat("qwen3:4b-instruct", [{"role": "user", "content": "用一句中文回答：一加一等于几？"}],
                        ChatOptions(max_tokens=64, think=False))
    assert res.text.strip() and "<think>" not in res.text
    assert res.ttft_ms is not None and res.ttft_ms < 30_000
    assert res.input_tokens and res.output_tokens


def test_end_to_end_with_real_models(tmp_path, corpus):
    app = App.open(home=tmp_path / "home")
    rep = app.indexer.index_folder(corpus)
    assert rep.files_indexed == 4
    ans = app.qa.ask("项目二期的预算上限是多少？")
    assert ans.citations, f"回答没有有效引用：{ans.text}"
    assert any(c.chunk.file_name in ("项目计划书.docx", "评审会议纪要.pdf") for c in ans.citations)
    app.close()
