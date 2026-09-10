"""R7: confirm-before-publish — safe defaults, abort paths, --yes bypass."""
import pytest

from automato.adapters.base import ExecutorError
from automato.adapters.publish.youtube_studio import _confirm_publish
from automato.settings import RunSettings


def test_confirm_disabled_proceeds_without_input(monkeypatch):
    s = RunSettings(publish_confirm=False)
    assert _confirm_publish(s, "T", "main", input_fn=lambda: (_ for _ in ()).throw(AssertionError())) is True


def test_confirm_yes_proceeds():
    s = RunSettings(publish_confirm=True)
    assert _confirm_publish(s, "T", "main",
                            input_fn=lambda _: "y\n") is True
    assert _confirm_publish(s, "T", "main",
                            input_fn=lambda _: "YES\n") is True


def test_confirm_no_aborts():
    s = RunSettings(publish_confirm=True)
    for answer in ("n", "no", "", "q"):
        with pytest.raises(ExecutorError, match="Aborted by operator"):
            _confirm_publish(s, "T", "main",
                             input_fn=lambda _: answer + "\n")


def test_confirm_non_interactive_aborts(monkeypatch):
    class NonTty:
        def isatty(self):
            return False

        def readline(self, prompt=""):
            return "y\n"

    monkeypatch.setattr("sys.stdin", NonTty())
    s = RunSettings(publish_confirm=True)
    with pytest.raises(ExecutorError, match="not interactive"):
        _confirm_publish(s, "T", "main")


def test_confirm_interactive_prompts_via_input(monkeypatch):
    # Regression: the interactive fallback called sys.stdin.readline(prompt),
    # which raised TypeError "'str' object cannot be interpreted as an integer"
    # on a real TextIOWrapper (readline treats the str as a buffer size).
    class Tty:
        def isatty(self):
            return True

        def readline(self, size=-1):
            if isinstance(size, str):
                raise TypeError("'str' object cannot be interpreted as an integer")
            return "y\n"

    monkeypatch.setattr("sys.stdin", Tty())
    s = RunSettings(publish_confirm=True)
    assert _confirm_publish(s, "T", "main") is True


def test_confirm_uses_settings_flag_for_yes_equivalent():
    # --yes pre-confirms by disabling the flag; publish proceeds un-prompted.
    s = RunSettings(publish_confirm=False)
    assert _confirm_publish(s, "T", "main", input_fn=None) is True


# --- row-text climbing so the success dialog's link is capturable ----------

def test_row_text_climbs_into_upload_dialog(monkeypatch):
    """The success dialog names the title and holds the video link in the SAME
    container; _row_text must climb to ytcp-uploads-dialog so the title-needle
    check passes. Regression: a newly-published short was missed because the
    extractor never searched the dialog container text."""
    from automato.adapters.publish.youtube_studio import _row_text

    class FakeLink:
        def evaluate(self, _code):
            return ("something else",)

    fake = FakeLink()
    monkeypatch.setattr(fake, "evaluate",
                        lambda code: "Find The Hidden Number Before Time Takes "
                                     "\u00b7 \u00b7 \u00b7 ytcp-uploads-dialog")
    assert "Find The Hidden" in _row_text(None, fake)
