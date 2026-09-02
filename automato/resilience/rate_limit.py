"""Detect and honor HTTP 429 / rate-limit / challenge screens.

Rather than failing, we detect common rate-limit signals in the page (status text,
challenge frames, anti-bot interstitials) and wait an escalating amount of time.
"""
from __future__ import annotations

import logging
import random
import time

from .. import config

log = logging.getLogger(__name__)

# Text fragments that indicate a rate limit / challenge on many providers.
RATE_LIMIT_PATTERNS = [
    "429",
    "rate limit",
    "too many requests",
    "slow down",
    "a moment",
    "limit reached",
    "temporary",
    "unusual traffic",
    "verify you are human",
    "cloudflare",
    "please wait",
    "try again later",
    "quota",
]


class RateLimitAwareWaiter:
    """Waits intelligently when a rate-limit or challenge is suspected."""

    def __init__(self, page):
        self._page = page

    def looks_rate_limited(self) -> bool:
        try:
            body = self._page.locator("body").inner_text(timeout=2000).lower()
            return any(p in body for p in RATE_LIMIT_PATTERNS)
        except Exception:  # noqa: BLE001
            return False

    def wait_out_rate_limit(self) -> None:
        """If the page looks rate-limited, sleep for an escalating window."""
        if not self.looks_rate_limited():
            return
        wait = random.uniform(config.RATE_LIMIT_MIN_WAIT_S, config.RATE_LIMIT_MAX_WAIT_S)
        log.warning("Rate limit / challenge detected; waiting %.1fs", wait)
        time.sleep(wait)
