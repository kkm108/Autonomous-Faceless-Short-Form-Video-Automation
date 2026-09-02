"""Per-provider session management: profile dirs, the pre-run auth check, and
one-time manual login assistance.

Each provider gets its own isolated Edge profile under ``profiles/<name>``. The
``auth_check`` function is called at the start of a run; if it fails, a clear
"re-authentication required" error is surfaced instead of a confusing locator error.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from .. import config
from .factory import PersistentBrowser

log = logging.getLogger(__name__)

# A provider's definition: profile name, and an auth-check that returns True if
# logged in. Adapters register their providers via register_provider().
@dataclass
class Provider:
    name: str
    auth_check: Optional[Callable[[object], bool]] = None  # (persistent_context) -> bool
    login_hint: str = ""


_REGISTRY: Dict[str, Provider] = {}


def register_provider(name: str, auth_check=None, login_hint: str = "") -> None:
    _REGISTRY[name] = Provider(name=name, auth_check=auth_check, login_hint=login_hint)


def profile_dir_for(name: str) -> Path:
    return config.PROFILES_DIR / name


def release_session_lock(provider_name: str) -> None:
    """Remove a provider's session.lock. Called when its browser is closed."""
    try:
        (profile_dir_for(provider_name) / "session.lock").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def open_session(provider_name: str, headless: Optional[bool] = None):
    """Open a persistent browser session for a provider.

    Returns a (PersistentBrowser, context) tuple. Caller is responsible for
    closing the PersistentBrowser.

    A per-profile lock file guards against two concurrent runs opening the same
    profile (which would corrupt the session / trip Chromium's profile lock). The
    lock is advisory: it warns, rather than blocking, so a stale lock (e.g. after
    a hard crash) never wedges the engine.
    """
    pdir = profile_dir_for(provider_name)
    lock = pdir / "session.lock"
    try:
        if lock.exists():
            age = time.time() - lock.stat().st_mtime
            if age < 600:
                log.warning(
                    "Profile '%s' already has a session.lock (%ds old); another run "
                    "may be active. Continuing -- if a second run really is touching "
                    "this profile, data may be lost.",
                    provider_name, int(age),
                )
    except Exception:  # noqa: BLE001
        pass
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"{time.time()}\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.warning("Could not write session.lock for %s", provider_name)

    browser = PersistentBrowser(pdir, headless=headless)
    context = browser.start()
    return browser, context


class AuthRequiredError(Exception):
    """Raised when a provider needs re-authentication before a run."""


def run_auth_check(provider_name: str, context) -> None:
    """Run a provider's auth check; raise AuthRequiredError if it fails."""
    provider = _REGISTRY.get(provider_name)
    if provider is None or provider.auth_check is None:
        return
    log.info("Running auth check for provider '%s'", provider_name)
    if not provider.auth_check(context):
        raise AuthRequiredError(
            f"Provider '{provider_name}' requires re-authentication.\n"
            f"{provider.login_hint}"
        )


def auth_required_hint(provider_name: str) -> str:
    provider = _REGISTRY.get(provider_name)
    return provider.login_hint if provider else ""
