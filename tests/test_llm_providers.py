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


class _FakeBox:
    """Scripted playwright locator stub for _type_into's input path."""

    def __init__(self, contenteditable, echo_fill=True):
        self._ce = contenteditable or ""
        self._echo_fill = echo_fill
        self._text = ""
        self.calls = []

    def get_attribute(self, name):
        return self._ce if name == "contenteditable" else None

    def click(self, *args, **kwargs):
        self.calls.append(("click",))

    def press_sequentially(self, text, **kwargs):
        self.calls.append(("press_sequentially", text))

    def fill(self, text, **kwargs):
        self._text = text if self._echo_fill else ""
        self.calls.append(("fill", text))

    def inner_text(self, **kwargs):
        return self._text


def test_contenteditable_typed_fallback_never_types_newlines():
    # R6 regression: Gemini's Quill editor submits on Enter, and
    # press_sequentially maps a literal \n to an Enter keypress. When the editor
    # ignores fill() and we must type, a multi-line prompt would fragment into
    # several premature sends. The typed fallback must collapse newlines.
    from automato.llm.no_login import _type_into

    box = _FakeBox("true", echo_fill=False)
    _type_into(object(), box, "TITLE | Hello\nNARRATION | world\n\nEND")
    typed = [text for kind, text in (c for c in box.calls if len(c) == 2)
             if kind == "press_sequentially"]
    assert typed
    assert all("\n" not in t and "\r" not in t for t in typed)
    assert "TITLE | Hello NARRATION | world  END" in typed  # words stay separated


def test_contenteditable_fill_path_preserves_newlines():
    # When the editor accepts fill(), the value is set atomically (no keystrokes,
    # so no Enter-key sends) and the prompt's line structure is kept intact for
    # the model's delimited format — no typed fallback occurs.
    from automato.llm.no_login import _type_into

    box = _FakeBox("true")
    _type_into(object(), box, "TITLE | Hello\nNARRATION | world\n\nEND")
    kinds = [kind for kind, *_ in box.calls]
    fills = [text for kind, text in (c for c in box.calls if len(c) == 2)
             if kind == "fill"]
    assert "press_sequentially" not in kinds
    assert fills == ["TITLE | Hello\nNARRATION | world\n\nEND"]


def test_textarea_path_preserves_newlines():
    # Non-contenteditable boxes take fill() directly, where literal newlines are
    # safe (no keystrokes fired) and must be preserved for the model's format.
    from automato.llm.no_login import _type_into

    box = _FakeBox("")
    _type_into(object(), box, "TITLE | Hello\nNARRATION | world\n\nEND")
    fills = [text for kind, text in (c for c in box.calls if len(c) == 2)
             if kind == "fill"]
    assert fills == ["TITLE | Hello\nNARRATION | world\n\nEND"]


def test_extract_last_gemini_reply_single_turn():
    from automato.llm.no_login import _extract_last_gemini_reply

    body = ("Conversation with Gemini\nYou said\nTITLE | hi\n\n"
            "Gemini said\nNARRATION | hello there\n\nFlash-Lite")
    assert _extract_last_gemini_reply(body) == "NARRATION | hello there\n\nFlash-Lite"


def test_extract_last_gemini_reply_prefers_latest_turn():
    from automato.llm.no_login import _extract_last_gemini_reply

    body = ("You said\nq one\nGemini said\nanswer one\n\n"
            "You said\nq two\nGemini said\nNARRATION | final answer\nEND")
    assert _extract_last_gemini_reply(body) == "NARRATION | final answer\nEND"


def test_extract_last_gemini_reply_no_label():
    from automato.llm.no_login import _extract_last_gemini_reply

    assert _extract_last_gemini_reply("no turn labels here") == ""
