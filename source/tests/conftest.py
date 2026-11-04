"""共享测试夹具。

所有测试都使用 FakeProvider，不依赖本机 Ollama；需要真实模型的测试在 tests/live/ 下，默认跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lkos.config import load_config
from lkos.storage.db import connect, init_schema
from lkos.storage.repository import Repository

TEST_DIM = 64


def fake_overrides(dim: int = TEST_DIM, **extra) -> dict:
    """把所有模型指向 FakeProvider 的配置覆盖。"""
    base = {
        "providers": {"test": {"type": "fake"}},
        "models": {
            "chat": {"test/fake-chat": {"context_window": 8192, "think": False}},
            "embedding": {"ref": "test/fake-embed", "dim": dim, "batch_size": 4},
        },
        "tasks": {"knowledge_qa": {"default_model": "test/fake-chat"}},
    }
    from lkos.config import deep_merge

    return deep_merge(base, extra)


@pytest.fixture
def cfg():
    return load_config(None, overrides=fake_overrides())


@pytest.fixture
def repo(tmp_path: Path):
    conn = connect(tmp_path / "lkos.db")
    init_schema(conn, "test/fake-embed", TEST_DIM)
    yield Repository(conn)
    conn.close()


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """三种格式、三种语言的小语料。"""
    from tests.samples import build_corpus

    root = tmp_path / "docs"
    build_corpus(root)
    return root
