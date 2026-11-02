import pytest

from lkos.config import load_config, split_model_ref
from lkos.errors import ConfigError
from tests.conftest import fake_overrides


def test_defaults_match_design():
    cfg = load_config(None)
    assert cfg.tasks["knowledge_qa"].default_model == "local/qwen3:4b-instruct"
    assert cfg.chat_model("local/qwen3:4b-instruct").context_window == 8192
    assert cfg.chat_model("local/qwen3:4b-instruct").think is False
    assert cfg.embedding.ref == "local/bge-m3" and cfg.embedding.dim == 1024
    assert cfg.privacy.remote_enabled is False
    assert cfg.privacy.offline_mode is True
    assert cfg.providers["hermes"].is_remote is True
    assert cfg.providers["local"].is_remote is False
    assert cfg.indexing.extensions == [".pdf", ".docx", ".md"]


def test_user_file_deep_merges_over_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("retrieval:\n  top_k: 7\nindexing:\n  chunk_max_chars: 500\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.retrieval.top_k == 7
    assert cfg.retrieval.rrf_k == 60  # 未覆盖的字段保留默认值
    assert cfg.indexing.chunk_max_chars == 500
    assert cfg.indexing.chunk_overlap_chars == 100


def test_missing_user_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg.retrieval.top_k == 5


def test_unregistered_chat_model_gets_default_params():
    cfg = load_config(None)
    m = cfg.chat_model("local/llama3:8b")
    assert m.ref == "local/llama3:8b" and m.context_window == 8192


@pytest.mark.parametrize("ref,expected", [
    ("local/qwen3:4b-instruct", ("local", "qwen3:4b-instruct")),
    ("hermes/org/model-x", ("hermes", "org/model-x")),
])
def test_split_model_ref(ref, expected):
    assert split_model_ref(ref) == expected


@pytest.mark.parametrize("bad", ["qwen3:4b", "/x", "local/", 3])
def test_split_model_ref_rejects_bad(bad):
    with pytest.raises(ConfigError):
        split_model_ref(bad)


def test_unknown_provider_type_rejected():
    with pytest.raises(ConfigError, match="不支持"):
        load_config(None, overrides={"providers": {"x": {"type": "openai"}}})


def test_task_referencing_undefined_provider_rejected():
    with pytest.raises(ConfigError, match="未定义的 provider"):
        load_config(None, overrides={"tasks": {"knowledge_qa": {"default_model": "nope/m"}}})


def test_overlap_must_be_smaller_than_chunk():
    with pytest.raises(ConfigError):
        load_config(None, overrides={"indexing": {"chunk_max_chars": 100, "chunk_overlap_chars": 100}})


def test_invalid_yaml_reports_config_error(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("retrieval: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(p)


def test_extensions_normalized():
    cfg = load_config(None, overrides={"indexing": {"extensions": ["PDF", ".MD"]}})
    assert cfg.indexing.extensions == [".pdf", ".md"]


def test_fake_overrides_valid():
    cfg = load_config(None, overrides=fake_overrides())
    assert cfg.providers["test"].type == "fake"
    assert cfg.embedding.dim == 64
