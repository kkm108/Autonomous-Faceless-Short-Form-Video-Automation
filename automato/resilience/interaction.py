"""Typed, resilient element interaction wrapper.

Every ambient browser interaction (click, fill, type, upload, wait) goes through
this thin layer. It composes the RetryPolicy, ModalDismisser and
RateLimitAwareWaiter so that the calling adapter code stays clean and the
robustness guarantees are applied uniformly.
"""
from __future__ import annotations

import logging
from typing import Optional

from .modal import ModalDismisser
from .rate_limit import RateLimitAwareWaiter
from .retry import retry
from . import recovery as recovery_mod
from . import challenge as challenge_mod
from .. import config

log = logging.getLogger(__name__)


class ElementInteractor:
    """Wraps a Playwright page with resilient interaction helpers."""

    def __init__(self, page, locs=None, provider: Optional[str] = None):
        self._page = page
        self._locs = locs
        self._provider = provider
        self._modals = ModalDismisser(page)
        self._rl = RateLimitAwareWaiter(page)

    # -- primitive helpers -------------------------------------------------
    def _with_prep(self, fn):
        """Dismiss modals + honor rate limits before *each* attempt of an action."""
        def guarded():
            self._modals.dismiss()
            self._rl.wait_out_rate_limit()
            return fn()
        return guarded

    def _recover(self, description: str, loc_group: Optional[str],
                 failed_selectors, value_hint: Optional[str] = None) -> bool:
        """Invoke the LLM recovery agent on failure. Returns True if a fix applied."""
        if not loc_group or not recovery_mod or recovery_mod.attempt_recover is None:
            return False
        desc = description if not value_hint else f"{description} (value: {value_hint})"
        return bool(recovery_mod.attempt_recover(
            self._page, desc, failed_selectors or [], loc_group, self._locs))

    def _guard(self, fn, description, loc_group, failed_selectors, value_hint=None):
        try:
            return fn()
        except Exception:
            if self._recover(description, loc_group, failed_selectors, value_hint):
                return None
            raise

    def click(self, locator, description="click", loc_group=None, force=False):
        failed = [_locator_hint(locator)]
        return self._guard(
            lambda: retry(
                self._with_prep(lambda: locator.click(timeout=10000, force=force)),
                description=f"{description}: {_locator_hint(locator)}",
            ),
            description, loc_group, failed,
        )

    def fill(self, locator, value: str, description="fill", loc_group=None):
        failed = [_locator_hint(locator)]
        return self._guard(
            lambda: retry(
                self._with_prep(lambda: locator.fill(value, timeout=10000)),
                description=f"{description}: {_locator_hint(locator)}",
            ),
            description, loc_group, failed, value_hint=value,
        )

    def type_text(self, locator, value: str, delay_ms: int = 30, description="type",
                  loc_group=None):
        failed = [_locator_hint(locator)]
        return self._guard(
            lambda: retry(
                self._with_prep(lambda: locator.type(value, delay=delay_ms)),
                description=f"{description}: {_locator_hint(locator)}",
            ),
            description, loc_group, failed, value_hint=value,
        )

    def upload(self, locator, file_path: str, description="upload", loc_group=None):
        failed = [_locator_hint(locator)]
        return self._guard(
            lambda: retry(
                self._with_prep(lambda: locator.set_input_files(file_path)),
                description=f"{description}: {_locator_hint(locator)}",
            ),
            description, loc_group, failed, value_hint=file_path,
        )

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout_ms: int = 60000,
             loc_group=None, challenge_check: bool = True):
        failed = [f"goto {url}"]

        def _inner():
            return retry(
                self._with_prep(lambda: self._page.goto(url, wait_until=wait_until, timeout=timeout_ms)),
                description=f"goto {url}",
            )

        result = self._guard(_inner, f"goto {url}", loc_group, failed)
        if challenge_check and config.CHALLENGE_CHECK and self._page:
            try:
                outcome = challenge_mod.check_and_gate(self._page, self._provider or "browser")
                if not outcome:
                    log.warning("Challenge unresolved after navigating to %s", url)
            except Exception as exc:  # noqa: BLE001
                log.warning("Challenge check skipped (%s)", exc)
        return result

    def wait_for(self, locator, timeout_ms: int = 30000, state: str = "visible",
                 loc_group=None):
        failed = [_locator_hint(locator)]
        return self._guard(
            lambda: retry(
                lambda: locator.wait_for(state=state, timeout=timeout_ms),
                description=f"wait_for {state}: {_locator_hint(locator)}",
                attempts=2,
            ),
            f"wait_for {state}", loc_group, failed,
        )

    def press(self, key: str, description="press"):
        return retry(
            self._with_prep(lambda: self._page.keyboard.press(key)),
            description=f"press {key}",
        )

    def input_value(self, locator, description="read input"):
        return retry(
            lambda: locator.input_value(timeout=8000),
            description=f"{description}: {_locator_hint(locator)}",
        )

    @property
    def page(self):
        return self._page


def _locator_hint(locator) -> str:
    try:
        return str(locator)[:140]
    except Exception:  # noqa: BLE001
        return "?"
