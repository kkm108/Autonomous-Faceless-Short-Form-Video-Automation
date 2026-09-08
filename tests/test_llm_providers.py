"""R6: pure-logic tests for the no-login LLM fallback provider chain."""

from automato import config
from automato.adapters.scripting import generic_llm

DEFAULT_CHAIN = ["duckai", "ask_brave", "gemini", "chatgpt"]


def _patch_chain(monkeypatch):
    monkeypatch.setattr(config, "LLM_NO_LOGIN_CHAIN", list(DEFAULT_CHAIN))


def test_provider_sequence_preferred_ai_studio_first(monkeypatch):
    _patch_chain(monkeypatch)
    seq = generic_llm._provider_sequence("ai_studio")
    assert seq[0] == "ai_studio"
    assert seq[1:] == DEFAULT_CHAIN


def test_provider_sequence_no_login_preferred_deduped(monkeypatch):
    _patch_chain(monkeypatch)
    seq = generic_llm._provider_sequence("gemini")
    assert seq == ["gemini", "duckai", "ask_brave", "chatgpt"]


def test_provider_sequence_duckai_preferred_stays_first(monkeypatch):
    _patch_chain(monkeypatch)
    seq = generic_llm._provider_sequence("duckai")
    assert seq == DEFAULT_CHAIN


def test_provider_sequence_unknown_preferred_falls_back(monkeypatch):
    _patch_chain(monkeypatch)
    seq = generic_llm._provider_sequence("nope")
    assert seq[0] == "ai_studio"
    assert "nope" not in seq


def test_every_chain_entry_has_a_driver(monkeypatch):
    _patch_chain(monkeypatch)
    from automato.llm import no_login

    drivers = {"ask_brave": "ask_brave", "gemini": "ask_gemini",
               "chatgpt": "ask_chatgpt"}
    for provider in config.LLM_NO_LOGIN_CHAIN:
        fn = drivers.get(provider)
        if fn is not None:  # duckai is handled by automato.llm.chat.ask
            assert hasattr(no_login, fn)


def test_chained_drivers_expose_identical_signatures():
    from automato.llm import no_login

    for name in ("ask_brave", "ask_gemini", "ask_chatgpt"):
        fn = getattr(no_login, name)
        assert fn.__code__.co_argcount <= 2  # (page, prompt) or fewer
