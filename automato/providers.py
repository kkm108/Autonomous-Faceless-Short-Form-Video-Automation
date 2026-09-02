"""Provider session registrations.

Here we register each browser-driven provider with its persistence profile name and
an optional auth check (returns True when logged in). Adapters and the orchestrator
consult this registry to open/validate each provider's persistent session.
"""
from __future__ import annotations

import logging

from .browser.session import register_provider

log = logging.getLogger(__name__)


def _youtube_auth_check(browser) -> bool:
    page = browser.first_page()
    try:
        page.goto("https://studio.youtube.com/", wait_until="domcontentloaded", timeout=45000)
        # If signed in, the "Create" button (ARIA Create) is present.
        page.locator("button[aria-label='Create']").wait_for(state="visible", timeout=12000)
        return True
    except Exception:  # noqa: BLE001
        return False


def _generic_login_check(url: str, selector: str) -> bool:
    def check(browser) -> bool:
        page = browser.first_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.locator(selector).wait_for(state="visible", timeout=12000)
            return True
        except Exception:  # noqa: BLE001
            return False
    return check


def register_all() -> None:
    register_provider(
        "youtube",
        auth_check=_youtube_auth_check,
        login_hint=(
            "Re-login YouTube: run `python -m automato login youtube`, sign in to "
            "your Google account in the visible browser, then close it."
        ),
    )
    # AI Studio / DeepSeek etc. can be used even without login, so auth is optional;
    # we still provide a login hint if the user chooses to log into a free tier.
    register_provider(
        "ai_studio",
        auth_check=None,
        login_hint=(
            "Optional: run `python -m automato login ai_studio` to sign into Google "
            "AI Studio for a more generous free quota."
        ),
    )
    # Perchance / TTS need no login at all.
    register_provider("perchance", auth_check=None,
                      login_hint="Perchance requires no login.")
    register_provider("tts", auth_check=None,
                      login_hint="In-browser TTS requires no login.")
    log.debug("Registered provider sessions.")
