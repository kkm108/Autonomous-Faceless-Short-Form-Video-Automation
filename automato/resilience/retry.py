"""Exponential backoff retry with jitter.

Wraps ambient browser interactions so that transient failures (element not found,
intermittent network hiccups, in-flight uploads) are retried intelligently rather
than surfacing as hard crashes.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Optional, TypeVar

from .. import config

log = logging.getLogger(__name__)
T = TypeVar("T")


class RetryExhausted(Exception):
    """Raised when an operation fails after all configured attempts."""

    def __init__(self, attempts: int, message: str):
        super().__init__(message)
        self.attempts = attempts


def _backoff_delay(attempt: int) -> float:
    base = config.RETRY_BASE_DELAY_S * (config.RETRY_BACKOFF ** attempt)
    jitter = random.uniform(0.0, config.RETRY_JITTER_S)
    return base + jitter


def retry(
    fn: Callable[[], T],
    attempts: Optional[int] = None,
    description: str = "operation",
    on_retry: Optional[Callable[[int, Exception], None]] = None,
) -> T:
    """Call ``fn`` with retry + backoff. ``fn`` should raise on failure and return
    the desired value on success."""
    attempts = attempts or config.RETRY_ATTEMPTS
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberate resilience catch-all
            last_exc = exc
            if attempt >= attempts:
                break
            delay = _backoff_delay(attempt)
            log.warning(
                "%s failed on attempt %d/%d (%s); retrying in %.1fs",
                description, attempt, attempts, exc, delay,
            )
            if on_retry:
                try:
                    on_retry(attempt, exc)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(delay)
    raise RetryExhausted(attempts, f"{description} failed after {attempts} attempts: {last_exc}")
