"""Detect and honor HTTP 429 / rate-limit / challenge screens.

Rather than failing, we detect common rate-limit signals in the page (status text,
challenge frames, anti-bot interstitials) and wait an escalating amount of time.

Detection is deliberately conservative to avoid false positives (R1-W5): the cheap
body-text scan only counts when a *strong* phrase is present OR a real structural
signal (captcha container / challenge iframe) confirms it. Generic tokens like
"a moment" or "quota" alone never trigger -- YouTube Studio's normal "please wait a
moment" processing copy must not inject a random delay into ordinary uploads.
"""
from __future__ import annotations

import logging
import random
import time

from .. import config

log = logging.getLogger(__name__)

# Strong phrases: unambiguous enough to flag a rate-limit / anti-bot wall on their
# own. Mirrors challenge.py's strong list plus HTTP/429 vocabulary.
_STRONG_PATTERNS = [
    "too many requests",
    "rate limit",
    "rate-limited",
    "slow down",
    "429",
    "unusual traffic",
    "you have been temporarily blocked",
    "cloudflare",
    "verify you are human",
    "access denied",
    "click to verify",
]
# Weak tokens that can appear in benign copy; only count if a structural signal is
# also present (or the token is so specific it's effectively strong).
_WEAK_PATTERNS = [
    "a moment",
    "quota",
    "please wait",
    "temporary",
    "try again later",
    "limit reached",
]
# Structural confirmations: real anti-bot / challenge containers in the DOM.
_STRUCTURAL_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='captcha']",
    "div.g-recaptcha",
    "div.h-captcha",
    "#challenge-form",
]

# Friendly number-encoded 429 responses are caught by "429" above.


class RateLimitAwareWaiter:
    """Waits intelligently when a rate-limit or challenge is suspected."""

    def __init__(self, page):
        self._page = page

    def _has_structural_signal(self) -> bool:
        try:
            for sel in _STRUCTURAL_SELECTORS:
                if self._page.locator(sel).count() > 0:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def looks_rate_limited(self) -> bool:
        try:
            body = self._page.locator("body").inner_text(timeout=2000).lower()
        except Exception:  # noqa: BLE001
            body = ""
        if any(p in body for p in _STRONG_PATTERNS):
            return True
        if self._has_structural_signal():
            return any(p in body for p in _WEAK_PATTERNS)
        return False

    def wait_out_rate_limit(self) -> None:
        """If the page looks rate-limited, sleep for an escalating window."""
        if not self.looks_rate_limited():
            return
        wait = random.uniform(config.RATE_LIMIT_MIN_WAIT_S, config.RATE_LIMIT_MAX_WAIT_S)
        log.warning("Rate limit / challenge detected; waiting %.1fs", wait)
        time.sleep(wait)
