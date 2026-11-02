import pytest

from lkos.config import load_config
from lkos.errors import EmbeddingMismatchError, ModelUnavailableError, RemoteDisabledError
from lkos.gateway.fake import FakeProvider
from lkos.gateway.gateway import ModelGateway
from tests.conftest import fake_overrides


class MemLogger:
    def __init__(self):
        self.entries = []

    def log_call(self, entry):
        self.entries.append(entry)


def make(privacy=None, providers=None, **extra):
    over = fake_overrides(**extra)
    over["providers"]["hermes"] = {"type": "hermes"}
    if privacy:
        over["privacy"] = privacy
    cfg = load_config(None, overrides=over)
    log = MemLogger()
    gw = ModelGateway(cfg, logger=log, providers=providers, snapshot=lambda: (1234, 0))
    return gw, log


MSG = [{"role": "user", "content": "资料：\n[1] (a.md)\n内容\n\n问题：x"}]


def test_uses_task_default_model_and_options():
    fake = FakeProvider("test")
    gw, log = make(providers={"test": fake})
    res, ref = gw.chat("knowledge_qa", MSG)
    assert ref == "test/fake-chat"
    call = fake.chat_calls[0]
    assert call["model"] == "fake-chat"
    assert call["opts"].num_ctx == 8192 and call["opts"].think is False
    assert call["opts"].temperature == 0.3 and call["opts"].max_tokens == 1024
    e = log.entries[-1]
    assert (e.task, e.provider, e.model, e.error) == ("knowledge_qa", "test", "fake-chat", None)
    assert e.mem_available_mb == 1234 and e.output_tokens is not None


def test_manual_override_wins():
    fake = FakeProvider("test")
    gw, _ = make(providers={"test": fake})
    _, ref = gw.chat("knowledge_qa", MSG, model_override="test/other-model")
    assert ref == "test/other-model" and fake.chat_calls[0]["model"] == "other-model"


@pytest.mark.parametrize("privacy,match", [
    ({"remote_enabled": False, "offline_mode": False}, "未开启"),
    ({"remote_enabled": True, "offline_mode": True}, "离线模式"),
])
def test_remote_model_blocked_by_privacy(privacy, match):
    gw, log = make(privacy=privacy)
    with pytest.raises(RemoteDisabledError, match=match):
        gw.chat("knowledge_qa", MSG, model_override="hermes/some-model")
    assert log.entries == []  # 未发出调用


def test_remote_allowed_reaches_provider_but_poc_not_implemented():
    gw, log = make(privacy={"remote_enabled": True, "offline_mode": False})
    with pytest.raises(ModelUnavailableError, match="尚未实现"):
        gw.chat("knowledge_qa", MSG, model_override="hermes/some-model")
    assert log.entries[-1].error


def test_local_failure_never_falls_back_to_remote():
    broken = FakeProvider("test", fail=True)
    gw, log = make(privacy={"remote_enabled": True, "offline_mode": False}, providers={"test": broken})
    with pytest.raises(ModelUnavailableError):
        gw.chat("knowledge_qa", MSG)
    assert [e.provider for e in log.entries] == ["test"]  # 只调用过本地
    assert log.entries[0].error


def test_unexpected_exception_is_wrapped_and_logged():
    class Weird(FakeProvider):
        def chat(self, *a, **k):
            raise RuntimeError("socket exploded")

    gw, log = make(providers={"test": Weird("test")})
    with pytest.raises(ModelUnavailableError, match="socket exploded"):
        gw.chat("knowledge_qa", MSG)
    assert "RuntimeError" in log.entries[-1].error


def test_embed_logs_and_checks_dimension():
    gw, log = make(providers={"test": FakeProvider("test", dim=64)})
    vecs = gw.embed(["预算", "budget"])
    assert len(vecs) == 2 and len(vecs[0]) == 64
    assert log.entries[-1].task == "embed"
    gw2, log2 = make(providers={"test": FakeProvider("test", dim=32)})
    with pytest.raises(EmbeddingMismatchError):
        gw2.embed(["x"])
    assert "维度" in log2.entries[-1].error


def test_streaming_callback_passed_through():
    gw, _ = make(providers={"test": FakeProvider("test")})
    pieces = []
    res, _ = gw.chat("knowledge_qa", MSG, on_token=pieces.append)
    assert "".join(pieces) == res.text


def test_check_reports_each_model():
    gw, _ = make()
    rows = gw.check()
    assert ("test/fake-embed", True, "FakeProvider") in rows
    assert all(ok for _, ok, _ in rows)


def test_fake_embedding_is_deterministic_and_semantic():
    f = FakeProvider("t", dim=64)
    a, b, c = f.embed("m", ["二期预算上限", "预算上限是多少", "machine à café"])
    assert a == f.embed("m", ["二期预算上限"])[0]
    cos = lambda x, y: sum(i * j for i, j in zip(x, y))  # noqa: E731
    assert cos(a, b) > cos(a, c)
