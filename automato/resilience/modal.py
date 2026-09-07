"""Dismiss transient browser interstitials that block automation.

Common blockers: cookie banners, "stay signed in?", notification prompts,
first-run tour overlays, ad/consent dialogs. We sweep for them before acting and
dismiss via semantic locators when found.

R3-W4: the sweep is provider-scoped and auditable, and can be opted out of. A
provider's specific dismissal table runs *before* the generic baseline list, and
each dismissal logs what was near the element it clicked so an accidental hit
against a legitimate, similarly-labeled control is visible in the logs instead of
being indistinguishable from a correct dismissal.
"""
from __future__ import annotations

import logging

from .. import config

log = logging.getLogger(__name__)

# Semantic dismissal locators tried in order. Prefers ARIA labels / text over IDs.
DISMISS_LOCATORS = [
    "button[aria-label='Close']",
    "button[aria-label='Dismiss']",
    "button[aria-label='Not now']",
    "button[aria-label='Reject all']",
    "button:has-text('Got it')",
    "button:has-text('Accept all')",
    "button:has-text('Reject all')",
    "button:has-text('I agree')",
    "button:has-text('No thanks')",
    "button:has-text('Dismiss')",
    "button:has-text('Close')",
    "button:has-text('Not now')",
    "button:has-text('Skip')",
    "[aria-label='Not now']",
    "text='X' >> visible=true",
]

# Provider-specific dismissal selectors, tried before the baseline. Only add a
# selector here when it names an interstitial chrome element that belongs to that
# provider's own UI (e.g. YouTube's iron-iconbutton wrapper around a close X).
PROVIDER_DISMISS_LOCATORS = {
    "youtube": [
        "tp-yt-iron-iconbutton[aria-label='Close']",
    ],
    "perchance": [
        "button[aria-label='Close']",
        "button[aria-label='Accept all']",
    ],
}


def _audit_hint(el, locator: str) -> str:
    """Nearby (container dialog / modal / body) text for post-hoc auditing."""
    try:
        return el.evaluate(
            "e => { "
            "  const c = e.closest('dialog, [role=\"dialog\"], [role=\"alertdialog\"], "
            "                       [class*=\"modal\"], [class*=\"dialog\"], body'); "
            "  if (!c) return ''; "
            "  const t = c.innerText || ''; "
            "  return t.replace(/\\s+/g, ' ').slice(0, 100).trim(); "
            "}"
        )
    except Exception:  # noqa: BLE001
        return ""


class ModalDismisser:
    """Keeps the page free of interstitials, scoped and audited.

    ``provider`` (R3-W4) narrows the dismissal list to that provider's own
    interstitial chrome first; ``enabled=None`` falls back to
    ``config.MODAL_DISMISS_ENABLED`` and lets an explicit False opt out entirely.
    """

    def __init__(self, page, provider: str | None = None,
                 enabled: bool | None = None):
        self._page = page
        self._provider = provider
        if enabled is None:
            enabled = config.MODAL_DISMISS_ENABLED
        self._enabled = enabled

    def _ordered_locators(self) -> list:
        extras = (PROVIDER_DISMISS_LOCATORS.get(self._provider) or [])
        if not extras:
            return list(DISMISS_LOCATORS)
        seen = set(extras)
        return extras + [s for s in DISMISS_LOCATORS if s not in seen]

    def dismiss(self) -> bool:
        """Attempt to dismiss any visible dismissible modal/banner.

        Returns True if an interstitial was dismissed.
        """
        if not self._enabled:
            log.debug("Modal dismissal disabled for provider %r", self._provider)
            return False
        for locator in self._ordered_locators():
            try:
                el = self._page.locator(locator).first
                if not el.is_visible(timeout=800) or not el.is_enabled(timeout=800):
                    continue
                label = ""
                try:
                    label = (el.get_attribute("aria-label")
                             or el.inner_text(timeout=600) or "")
                except Exception:  # noqa: BLE001
                    label = ""
                near = _audit_hint(el, locator)
                el.click(timeout=1500)
                log.info("Dismissed %r via <%s> on provider %r near %r",
                         " ".join(label.split())[:40], locator,
                         self._provider, near)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def guard(self):
        """Return self usable as a context manager (does nothing on exit)."""
        return _GuardContext(self)

    def dismiss_after(self):
        self.dismiss()


class _GuardContext:
    def __init__(self, dismisser: ModalDismisser):
        self._d = dismisser

    def __enter__(self):
        self._d.dismiss()
        return self._d

    def __exit__(self, *exc):
        self._d.dismiss()
        return False