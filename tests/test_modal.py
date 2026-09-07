"""R3-W4: provider-scoped modal dismissal, auditing, and opt-out."""
import logging

from automato import config
from automato.resilience import modal as modal_mod


class _El:
    def __init__(self, label="Close", near="cookie consent banner"):
        self._label = label
        self._near = near
        self.clicked = False

    def is_visible(self, timeout=800):
        return True

    def is_enabled(self, timeout=800):
        return True

    def get_attribute(self, name):
        return self._label if name == "aria-label" else None

    def inner_text(self, timeout=600):
        return self._label

    def evaluate(self, js):
        return self._near

    def click(self, timeout=1500):
        self.clicked = True


class _Page:
    def __init__(self, els):
        self._els = els  # selector -> _El

    def locator(self, sel):
        el = self._els.get(sel)
        if el is None:
            class _Missing:
                def is_visible(self, timeout=800):
                    return False

                def is_enabled(self, timeout=800):
                    return False

            _Missing.__name__ = "Missing"
            return _First(_Missing())
        return _First(el)


class _First:
    def __init__(self, el):
        self._el = el

    @property
    def first(self):
        return self._el


def test_provider_scoping_tries_provider_list_first():
    # YouTube-specific close wrapper exists; the generic baseline would also match
    # "button[aria-label='Close']" -- but the provider-scoped list must win.
    yt = _El(near="YouTube upload dialog")
    page = _Page({"tp-yt-iron-iconbutton[aria-label='Close']": yt,
                  "button[aria-label='Close']": _El(near="somewhere else")})
    d = modal_mod.ModalDismisser(page, provider="youtube")
    assert d.dismiss() is True
    assert yt.clicked is True


def test_generic_scope_does_not_click_provider_specific():
    # Without the provider, the YouTube-only selector must not be touched.
    yt = _El()
    page = _Page({"tp-yt-iron-iconbutton[aria-label='Close']": yt})
    d = modal_mod.ModalDismisser(page, provider=None)
    assert d.dismiss() is False
    assert yt.clicked is False


def test_dismissal_logs_audit_context(caplog):
    el = _El(label="Close", near="Cookie settings from example.com")
    page = _Page({"button[aria-label='Close']": el})
    with caplog.at_level(logging.INFO, logger="automato.resilience.modal"):
        modal_mod.ModalDismisser(page).dismiss()
    assert el.clicked
    assert any("Dismissed" in r.getMessage() for r in caplog.records)
    assert any("Cookie settings from example.com" in r.getMessage()
               for r in caplog.records)


def test_dismiss_opt_out_does_nothing():
    el = _El()
    page = _Page({"button[aria-label='Close']": el})
    assert modal_mod.ModalDismisser(page, enabled=False).dismiss() is False
    assert el.clicked is False


def test_config_opt_out_default(monkeypatch):
    monkeypatch.setattr(config, "MODAL_DISMISS_ENABLED", False)
    el = _El()
    page = _Page({"button[aria-label='Close']": el})
    assert modal_mod.ModalDismisser(page).dismiss() is False
    assert el.clicked is False
