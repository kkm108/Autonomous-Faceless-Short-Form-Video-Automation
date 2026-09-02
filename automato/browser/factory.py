"""Persistent browser context factory built on Playwright.

Launches a persistent context bound to a per-provider profile dir, so cookies /
localStorage survive between runs — the mechanism satisfying the "no repeated
manual logins" invariant.

Browser + headless are configurable via config.BROWSER_CHOICE / HEADLESS_MODE:
  * edge / chrome  -> launched via Playwright ``channel`` ("msedge"/"chrome")
  * brave / custom -> launched via ``exe_path`` to the native binary
  * headless mode  -> "headed", "new" (--headless=new, looks like a real browser),
    or "full" (classic fully-headless)
Anti-bot flags are applied unless config.ANTI_AUTOMATION is False.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright

from .. import config

log = logging.getLogger(__name__)

# Playwright channel strings for channel-based launches.
_CHANNELS = {"edge": "msedge", "chrome": "chrome"}


def make_launch_args(browser_choice: Optional[str] = None,
                     headless_mode: Optional[str] = None) -> tuple:
    """Resolve launch kwargs/argv for a given choice/mode without launching.

    Returns (kwargs, extra_argv). Primarily used for tests/diagnostics.
    """
    choice = browser_choice or getattr(config, "BROWSER_CHOICE", "edge")
    mode = headless_mode or getattr(config, "HEADLESS_MODE", "headed")
    kwargs, extra_argv = _launch_params_for(choice, mode)
    return kwargs, extra_argv


class PersistentBrowser:
    """Owns one Playwright persistent context bound to a provider profile dir."""

    def __init__(self, profile_dir: Path, headless: Optional[bool] = None,
                 viewport: Optional[dict] = None, browser_choice: Optional[str] = None,
                 headless_mode: Optional[str] = None):
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = profile_dir
        self.viewport = viewport or config.VIEWPORT
        self.browser_choice = browser_choice or config.BROWSER_CHOICE
        self.headless_mode = headless_mode or config.HEADLESS_MODE
        self.headless = headless if headless is not None else (self.headless_mode != "headed")
        self._pw = None
        self._context = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        self._pw = sync_playwright().start()
        kwargs, extra_argv = _launch_params_for(self.browser_choice, self.headless_mode)

        if config.ANTI_AUTOMATION:
            kwargs.setdefault("args", []).extend([
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--no-default-browser-check",
            ])
        if extra_argv:
            kwargs.setdefault("args", []).extend(extra_argv)
        kwargs.setdefault("args", kwargs.get("args") or [])

        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport=self.viewport,
            **{k: v for k, v in kwargs.items() if k != "headless"},
        )
        log.debug("Launched persistent context at %s (browser=%s, headless=%s)",
                  self.profile_dir, self.browser_choice, self.headless)
        return self._context

    @property
    def pages(self):
        if self._context is None:
            raise RuntimeError("Browser not started; call start() or use as context manager")
        return self._context.pages

    def first_page(self):
        pages = self.pages
        return pages[0] if pages else self._context.new_page()

    def close(self):
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._context = None
            self._pw = None


def _launch_params_for(browser_choice: str, headless_mode: str):
    """Like _launch_params but driven by explicit args (no config mutation)."""
    kwargs = {}
    if browser_choice in _CHANNELS:
        kwargs["channel"] = _CHANNELS[browser_choice]
    else:
        exe = getattr(config, "BROWSER_EXECUTABLE", {}).get(browser_choice)
        if exe and Path(exe).exists():
            kwargs["executable_path"] = exe
        elif browser_choice == "brave":
            log.warning("Brave binary not found; falling back to bundled chromium")
    extra_argv = []
    if headless_mode == "full":
        pass  # handled via headless=True
    elif headless_mode == "new":
        extra_argv.append("--headless=new")
    return kwargs, extra_argv
