"""Dismiss transient browser interstitials that block automation.

Common blockers: cookie banners, "stay signed in?", notification prompts,
first-run tour overlays, ad/consent dialogs. We sweep for them before acting and
dismiss via semantic locators when found.
"""
from __future__ import annotations

import logging

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
    "tp-yt-iron-iconbutton[aria-label='Close']",
]


class ModalDismisser:
    """Context manager / utility that keeps the page free of interstitials."""

    def __init__(self, page):
        self._page = page

    def dismiss(self) -> None:
        """Attempt to dismiss any visible dismissible modal/banner."""
        for locator in DISMISS_LOCATORS:
            try:
                el = self._page.locator(locator).first
                if el.is_visible(timeout=800) and el.is_enabled(timeout=800):
                    el.click(timeout=1500)
                    log.info("Dismissed interstitial via: %s", locator)
                    return
            except Exception:  # noqa: BLE001
                continue

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
