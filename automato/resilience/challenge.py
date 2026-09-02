"""Detect and gate on CAPTCHA / 2FA / unexpected login challenges.

Policy: we never try to auto-solve CAPTCHAs (ToS risk + unreliable). Instead we
``detect_challenge`` after navigation, and if one is found we ``wait_for_human``:
the run pauses, clear instructions are printed, and the person solves it in the
visible (headed) browser. Completion is signalled by creating the done-flag file or
simply by the challenge disappearing. This composes cleanly with ``--resume``.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from .. import config

log = logging.getLogger(__name__)

# Very strong, unambiguous signals -- checked on their own. These are unlikely to
# appear in benign page copy.
_STRONG_PATTERNS = ["recaptcha", "hcaptcha", "not a robot", "verify you are human",
                    "are you human", "confirm it's you", "unusual traffic",
                    "security check"]
# Weaker tokens that can appear in ordinary text ("2FA", "captcha", "verification
# code"); only treated as a challenge when a structural signal is also present.
_WEAK_PATTERNS = ["captcha", "two-factor", "2 factor", "2fa", "verification code",
                  "enter the code", "enter code", "authenticator app"]
# URL fragments that strongly indicate an auth wall.
_AUTH_URL_MARKERS = ["/login", "/signin", "/sign-in", "/2fa", "/verify",
                     "accounts.google.com/signin"]
# Challenge CAPTCHA containers (iframes / providers) that confirm a real wall.
_CHALLENGE_IFRAME_SELECTORS = [
    "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
    "iframe[src*='captcha']", "div.g-recaptcha", "div.h-captcha",
]


def _has_challenge_ui(page) -> bool:
    """Structural check: is there a real CAPTCHA container in the DOM?"""
    try:
        for sel in _CHALLENGE_IFRAME_SELECTORS:
            if page.locator(sel).count() > 0:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def detect_challenge(page) -> Optional[str]:
    """Return a challenge description if the current page looks blocked, else None.

    Conservative to avoid false positives on ordinary page copy: the cheap body-text
    scan only counts when a strong, unambiguous phrase is present OR when a real
    CAPTCHA container exists OR the URL itself is an auth wall. Weak tokens alone
    (e.g. the word "2FA" in a footnote) never trigger the human gate.
    """
    ui = _has_challenge_ui(page)
    try:
        text = page.locator("body").inner_text(timeout=3000) or ""
    except Exception:  # noqa: BLE001
        text = ""
    low = text.lower()
    for pat in _STRONG_PATTERNS:
        if pat in low:
            return f"Detected challenge marker: {pat!r}"
    if ui:
        for pat in _WEAK_PATTERNS:
            if pat in low:
                return f"Detected challenge marker: {pat!r}"
        return "Detected a challenge/CAPTCHA container in the page"
    try:
        url = page.url.lower()
        for frag in _AUTH_URL_MARKERS:
            if frag in url:
                return f"Navigated to an auth wall (URL contains {frag!r})"
    except Exception:  # noqa: BLE001
        pass
    return None


def _flag_path() -> Path:
    return Path(config.HUMAN_DONE_FLAG)


def wait_for_human(page, instructions: str, timeout_s: Optional[int] = None) -> bool:
    """Pause until the human signals they've finished, or the challenge clears.

    Returns True once the challenge is gone. Blocks up to ``timeout_s``.
    """
    timeout_s = timeout_s or config.CHALLENGE_PAUSE_SECONDS
    flag = _flag_path()
    try:
        flag.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass

    print("\n" + "=" * 70)
    print("BLOCKED BY A CHALLENGE (CAPTCHA / 2FA / LOGIN)")
    print(instructions)
    print("Do it in the open browser window, then create the flag file:")
    print(f"    {flag}")
    print(f"or it will keep checking for up to {timeout_s}s.")
    print("=" * 70 + "\n")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2)
        if flag.exists():
            try:
                flag.unlink()
            except Exception:  # noqa: BLE001
                pass
            print("Human-completion signal received.")
            return True
        # If the challenge disappeared by itself, no need to wait.
        if detect_challenge(page) is None:
            return True
    return False


def check_and_gate(page, provider: str = "provider") -> bool:
    """One-shot: detect a challenge and, if present, pause for the human.

    Returns True if the page is clear (no challenge, or resolved), False otherwise.
    """
    reason = detect_challenge(page)
    if reason is None:
        return True
    log.info("%s for %s", reason, provider)
    return wait_for_human(
        page,
        f"The {provider} session needs your help. Please complete the challenge "
        f"in the open browser, then signal you are done.",
    )
