"""R1-F1 / R1-W5 / R1-W6: challenge & rate-limit detection tests."""
import pytest

from automato import config
from automato.resilience import challenge, rate_limit


class _Page:
    """Stub page. Controls body text, url, and a single "container" presence."""

    def __init__(self, body="", url="https://example.com/", has_iframe=False):
        self._body = body
        self._url = url
        self._has_iframe = has_iframe

    @property
    def url(self):
        return self._url.lower()

    def locator(self, sel):
        class _L:
            def __init__(self, n, body):
                self._n = n
                self._body = body

            def count(self):
                return self._n

            def inner_text(self, timeout=None):
                return self._body

        structural = (
            set(challenge._CHALLENGE_IFRAME_SELECTORS)
            | set(rate_limit._STRUCTURAL_SELECTORS)
        )
        if sel in structural:
            return _L(1 if self._has_iframe else 0, self._body)
        if sel == "body":
            return _L(1, self._body)
        return _L(0, "")

    def get_attribute(self, *a, **k):
        return None


def test_detect_challenge_none_on_benign_page():
    p = _Page(body="Welcome back to your dashboard.")
    assert challenge.detect_challenge(p) is None


def test_detect_challenge_strong_phrase():
    p = _Page(body="Please verify you are human to continue.")
    assert challenge.detect_challenge(p) is not None


def test_detect_challenge_weak_token_ignored_without_structure():
    # "2FA" appears in ordinary copy but there's no captcha container or auth URL.
    p = _Page(body="Enable two-factor for security by going to /settings.")
    assert challenge.detect_challenge(p) is None


def test_detect_challenge_weak_token_counts_with_container():
    p = _Page(body="Enter the captcha below.", has_iframe=True)
    assert challenge.detect_challenge(p) is not None


def test_detect_challenge_auth_url():
    p = _Page(url="https://accounts.google.com/signin/v2/identifier")
    assert challenge.detect_challenge(p) is not None


def test_rate_limited_none_on_benign_page():
    p = _Page(body="Your video is processing, please wait a moment.")
    waiter = rate_limit.RateLimitAwareWaiter(p)
    # Weak generic token ("a moment") alone must not flag.
    assert waiter.looks_rate_limited() is False


def test_rate_limited_strong_signal():
    p = _Page(body="Too many requests. Slow down and try again later.")
    waiter = rate_limit.RateLimitAwareWaiter(p)
    assert waiter.looks_rate_limited() is True


def test_rate_limited_429_code_signal():
    p = _Page(body="Response status: 429")
    waiter = rate_limit.RateLimitAwareWaiter(p)
    assert waiter.looks_rate_limited() is True


def test_check_and_gate_raises_challenge_error_in_headless():
    p = _Page(body="Please verify you are human to continue.")
    prev = config.HEADLESS_MODE
    config.HEADLESS_MODE = "new"
    try:
        with pytest.raises(challenge.ChallengeError):
            challenge.check_and_gate(p, "youtube")
    finally:
        config.HEADLESS_MODE = prev


def test_check_and_gate_returns_true_when_clear():
    p = _Page(body="Welcome back.")
    assert challenge.check_and_gate(p, "youtube") is True
