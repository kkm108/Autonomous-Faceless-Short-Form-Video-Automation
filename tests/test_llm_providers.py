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


def test_extract_last_turn_single():
    from automato.llm.no_login import _extract_last_turn

    body = ("Conversation with Gemini\nYou said\nTITLE | hi\n\n"
            "Gemini said\nNARRATION | hello there\n\nFlash-Lite")
    out = _extract_last_turn(body, "Gemini said", "You said")
    assert out == "NARRATION | hello there\n\nFlash-Lite"


def test_extract_last_turn_prefers_latest():
    from automato.llm.no_login import _extract_last_turn

    body = ("You said\nq one\nGemini said\nanswer one\n\n"
            "You said\nq two\nGemini said\nNARRATION | final answer\nEND")
    out = _extract_last_turn(body, "Gemini said", "You said")
    assert out == "NARRATION | final answer\nEND"


def test_extract_last_turn_no_label_uses_fallback():
    from automato.llm.no_login import _extract_last_turn

    body = "You said\nlegacy UI prompt\nreply continues here"
    out = _extract_last_turn(body, "Gemini said", "You said")
    assert out == "legacy UI prompt\nreply continues here"


def test_extract_last_turn_neither_label():
    from automato.llm.no_login import _extract_last_turn

    assert _extract_last_turn("no turn labels here", "Gemini said", "") == ""


def test_extract_last_turn_chatgpt_label():
    from automato.llm.no_login import _extract_last_turn

    body = ("two benefits of morning sunlight is one short title / \n"
            "ChatGPT said:\ntwo short lines and nothing else\nEND")
    out = _extract_last_turn(body, "ChatGPT said:", "")
    assert out.startswith("two short lines")
    assert "END" in out


def test_locate_last_echo_softwrapped():
    # innerText adds soft-wrap newlines the source text doesn't have; the search
    # is whitespace-normalized and must still land just past the echo.
    from automato.llm.no_login import _locate_last_echo

    tail = "TITLE| and caption relate to topic."
    body = ("before\nyou said everything\n"
            "TITLE| and cap\ntion relate to top\nic.\nGemini said\nNARRATION| done")
    end = _locate_last_echo(body, tail)
    assert end > 0
    seg = body[end:].lstrip("\r\n \t")
    assert seg.startswith("Gemini said")


def test_locate_last_echo_picks_latest_echo():
    from automato.llm.no_login import _locate_last_echo

    tail = "same prompt tail"
    body = ("You said\nsame prompt tail\nGemini said\nold answer\n\n"
            "You said\nsame prompt tail\nGemini said\nnew answer")
    end = _locate_last_echo(body, tail)
    seg = body[end:].lstrip("\r\n \t")
    assert seg.startswith("Gemini said")
    assert "new answer" in seg and "old answer" not in seg


def test_locate_last_echo_missing():
    from automato.llm.no_login import _locate_last_echo

    assert _locate_last_echo("no echo here", "some tail") == -1
    assert _locate_last_echo("", "tail") == -1


def test_extract_current_reply_gemini_and_chatgpt():
    from automato.llm.no_login import _extract_current_reply

    tail = "caption must clearly relate to morning sunlight."
    gem_body = ("You said\nother stuff\n"
                "TITLE| x\ncaption must clearly relate to morning sunlight.\n"
                "Gemini said\nNARRATION| hello world\nEND")
    out = _extract_current_reply(gem_body, "Gemini said", tail)
    assert out == "NARRATION| hello world\nEND"

    gpt_body = ("earlier\n"
                "caption must clearly relate to morning sunlight.\n"
                "ChatGPT said:\nNARRATION| aptly answered\nEND")
    out = _extract_current_reply(gpt_body, "ChatGPT said:", tail)
    assert out == "NARRATION| aptly answered\nEND"
