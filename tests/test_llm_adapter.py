from datetime import date
from types import SimpleNamespace

import httpx
import pytest

import app.adapters.llm as llm
from app.config import load_config


class _FakeOpenAI:
    calls: list = []
    content = '{"ok": true}'
    finish = "stop"

    inits: list = []

    def __init__(self, **kw):
        _FakeOpenAI.inits.append(kw)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        _FakeOpenAI.calls.append(kw)
        msg = SimpleNamespace(content=_FakeOpenAI.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=_FakeOpenAI.finish)])


@pytest.fixture
def fake_openai(monkeypatch):
    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    _FakeOpenAI.calls, _FakeOpenAI.inits, _FakeOpenAI.content, _FakeOpenAI.finish = [], [], '{"ok": true}', "stop"
    return _FakeOpenAI


def cfg(**kw):
    return load_config(sample_mode=False, today=date(2026, 10, 26), llm_api_key="k", llm_model="nvidia/test", **kw)


def test_reasoning_is_switched_off_by_default(fake_openai):
    assert llm.openai_compatible_generator(cfg(llm_reasoning=False))("p", "s") == '{"ok": true}'
    assert fake_openai.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert fake_openai.calls[0]["model"] == "nvidia/test"


def test_reasoning_can_be_enabled(fake_openai):
    llm.openai_compatible_generator(cfg(llm_reasoning=True))("p", "s")
    assert fake_openai.calls[0]["extra_body"] is None


def test_empty_reply_falls_back_and_is_reported(fake_openai):
    fake_openai.content, fake_openai.finish = "", "length"
    notes = []
    gen = llm.with_fallback(llm.openai_compatible_generator(cfg()), lambda p, s: "fallback", notes.append)
    assert gen("p", "s") == "fallback"
    assert "EmptyReply" in notes[0] and "finish_reason=length" in notes[0]



@pytest.mark.parametrize("pasted", [
    "127.0.0.1:8088", "http://127.0.0.1:8088/", "http://127.0.0.1:8088/v1", "http://127.0.0.1:8088/v1/",
    "http://127.0.0.1:8088/v1/chat/completions", "http://127.0.0.1:8088/v1/models", "http://127.0.0.1:8088/healthz",
    "http://127.0.0.1:8088/v1/v1",
])
def test_meterless_url_is_normalised_to_host_v1(pasted):
    assert llm.normalize_meterless_url(pasted) == "http://127.0.0.1:8088/v1"


def test_meterless_generator_follows_the_runtime_contract(fake_openai):
    gen = llm.meterless_generator(cfg(meterless_token="tok-123", llm_base_url="127.0.0.1:8088"))
    assert gen("p", "s") == '{"ok": true}'
    init = fake_openai.inits[0]
    assert init["base_url"] == "http://127.0.0.1:8088/v1" and init["api_key"] == "tok-123"
    assert init["default_headers"] == {"meterless-app": "preflight", "meterless-scope": "user"}
    assert init["timeout"] == 180
    assert fake_openai.calls[0]["model"] == "meterless/auto" and fake_openai.calls[0]["extra_body"] is None


def test_live_chain_is_nightshift_then_nvidia_direct_then_rules(fake_openai):
    gen, name = llm.make_generator(cfg(meterless_token="tok", llm_base_url="http://127.0.0.1:8088/v1"), lambda n: None)
    assert name == "meterless:meterless/auto (fallback: nvidia-direct -> rule-based)"
    notes = []
    fake_openai.content, fake_openai.finish = "", "length"  # every provider returns empty
    gen, _ = llm.make_generator(cfg(meterless_token="tok", llm_base_url="http://127.0.0.1:8088/v1"), notes.append)
    prompt = '<input>{"text": "Ward 4B needs night cover tomorrow", "today": "2026-10-26"}</input>'
    assert '"ward": "4B"' in gen(prompt, "TASK: parse_request")  # answered by the rule-based fallback
    assert notes[0].startswith("meterless:meterless/auto failed") and notes[1].startswith("nvidia-direct:nvidia/test failed")


def test_token_with_nvidia_base_url_is_a_config_error():
    from app.config import ConfigError

    with pytest.raises(ConfigError, match="Nightshift"):
        llm.make_generator(cfg(meterless_token="tok", llm_base_url="https://integrate.api.nvidia.com/v1"), lambda n: None)


@pytest.mark.parametrize("response, status", [
    (httpx.Response(200, json={"data": [{"id": "meterless/auto"}, {"id": "meterless/fast"}]}), "online"),
    (httpx.Response(401, json={"error": "bad token"}), "unauthorized"),
    (httpx.Response(403), "unauthorized"),
    (httpx.Response(200, json={"data": [{"id": "gpt-4o"}]}), "offline"),
    (httpx.Response(500), "offline"),
])
def test_provider_status_is_decided_by_v1_models(response, status):
    seen = {}

    def handler(request):
        seen["auth"], seen["url"] = request.headers["authorization"], str(request.url)
        return response

    out = llm.provider_status(cfg(meterless_token="tok", llm_base_url="127.0.0.1:8088"),
                              client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out["status"] == status and seen["url"] == "http://127.0.0.1:8088/v1/models"
    assert seen["auth"] == "Bearer tok" and "tok" not in str(out)


def test_provider_status_offline_when_nothing_listens():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    out = llm.provider_status(cfg(meterless_token="tok"), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out["status"] == "offline" and out["detail"] == "ConnectError"
