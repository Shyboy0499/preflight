from datetime import date
from types import SimpleNamespace

import pytest

import app.adapters.llm as llm
from app.config import load_config


class _FakeOpenAI:
    calls: list = []
    content = '{"ok": true}'
    finish = "stop"

    def __init__(self, **kw):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        _FakeOpenAI.calls.append(kw)
        msg = SimpleNamespace(content=_FakeOpenAI.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=_FakeOpenAI.finish)])


@pytest.fixture
def fake_openai(monkeypatch):
    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    _FakeOpenAI.calls, _FakeOpenAI.content, _FakeOpenAI.finish = [], '{"ok": true}', "stop"
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
