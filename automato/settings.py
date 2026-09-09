"""Typed, validated, per-run settings (R2-W2 / R2-F2).

Replaces the old pattern of mutating the ``automato.config`` module in place at
runtime (``config.DEFAULT_VISIBILITY = args.visibility``) — which made tests
order-dependent, blocked testing two configurations in one process, and let a
typo'd value like "auto_" fail deep inside an adapter instead of at startup.

A :class:`RunSettings` is built **once per run** from: defaults <- environment
variables <- CLI flags, then validated so an out-of-range value raises
:class:`SettingsError` immediately. It is threaded explicitly through
:class:`automato.adapters.base.AdapterContext` (``ctx.settings``) instead of
being read from a shared mutable global.

Environment overrides (each also has a CLI flag):
  * ``AUTOMATO_VISIBILITY``       (public|unlisted|private)
  * ``AUTOMATO_TTS_PROVIDER``     (auto|soundtools|edge_tts|pyttsx3)
  * ``AUTOMATO_BROWSER``          (edge|chrome|brave|chromium)
  * ``AUTOMATO_HEADLESS_MODE``    (headed|new|full)
  * ``AUTOMATO_RECOVERY_ENABLED`` (true/false)
  * ``AUTOMATO_CHALLENGE_CHECK``  (true/false)
  * ``AUTOMATO_ANTI_AUTOMATION``  (true/false)

``AUTOMATO_LLM_PROVIDER`` (no CLI flag) overrides the scripting LLM provider:
  ``ai_studio`` or any no-login chain entry (``duckai``/``ask_brave``/``gemini``/
  ``chatgpt``). Setting a no-login provider starts the scripting chain straight at
  that provider, i.e. it *skips* AI Studio entirely (used for chain validation).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from . import config

log = logging.getLogger(__name__)

Visibility = Literal["public", "unlisted", "private"]
TtsProvider = Literal["auto", "soundtools", "edge_tts", "pyttsx3"]
BrowserChoice = Literal["edge", "chrome", "brave", "chromium"]
HeadlessMode = Literal["headed", "new", "full"]

_VISIBILITIES: List[str] = ["public", "unlisted", "private"]
_TTS_PROVIDERS: List[str] = ["auto", "soundtools", "edge_tts", "pyttsx3"]
_BROWSERS: List[str] = ["edge", "chrome", "brave", "chromium"]
_HEADLESS_MODES: List[str] = ["headed", "new", "full"]


class SettingsError(ValueError):
    """A per-run setting has an out-of-range value (caught at build time)."""


def _require(value: Any, allowed: List[str], name: str) -> Any:
    if value is None:
        return None
    if value not in allowed:
        raise SettingsError(
            f"Invalid {name}={value!r}. Must be one of: {', '.join(allowed)}. "
            f"This is caught here at startup, not deep inside an adapter "
            f"(R2-F2)."
        )
    return value


def _require_bool(value: Any, name: str) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    raise SettingsError(f"Invalid {name}={value!r}. Must be a boolean.")


def env_bool(name: str, default: bool) -> bool:
    """Read a boolean from the environment with clear validation errors."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    conv = _require_bool(raw, name)
    return default if conv is None else conv


@dataclass
class RunSettings:
    """Validated per-run configuration. Built once by :meth:`build`."""

    visibility: Visibility = "unlisted"
    tts_provider: TtsProvider = "auto"
    browser_choice: BrowserChoice = "edge"
    headless_mode: HeadlessMode = "headed"
    recovery_enabled: bool = True
    challenge_check: bool = True
    anti_automation: bool = True
    llm_provider: str = "ai_studio"
    # Mutable during a run: a soft-fail quality gate flips this to downgrade an
    # explicitly-requested public publish to unlisted as the last safety net
    # (R2-W4 / R2-F4).
    force_unlisted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _require(self.visibility, _VISIBILITIES, "visibility")
        _require(self.tts_provider, _TTS_PROVIDERS, "tts_provider")
        _require(self.browser_choice, _BROWSERS, "browser_choice")
        _require(self.headless_mode, _HEADLESS_MODES, "headless_mode")

    # -- construction ------------------------------------------------------

    @classmethod
    def defaults(cls) -> "RunSettings":
        """Settings assembled from code defaults + environment overrides only."""
        return cls(
            visibility=_require(os.environ.get("AUTOMATO_VISIBILITY",
                                               config.DEFAULT_VISIBILITY),
                                _VISIBILITIES, "AUTOMATO_VISIBILITY"),
            tts_provider=_require(os.environ.get("AUTOMATO_TTS_PROVIDER",
                                                 config.TTS_PROVIDER),
                                  _TTS_PROVIDERS, "AUTOMATO_TTS_PROVIDER"),
            browser_choice=_require(os.environ.get("AUTOMATO_BROWSER",
                                                   config.BROWSER_CHOICE),
                                    _BROWSERS, "AUTOMATO_BROWSER"),
            headless_mode=_require(os.environ.get("AUTOMATO_HEADLESS_MODE",
                                                  config.HEADLESS_MODE),
                                   _HEADLESS_MODES, "AUTOMATO_HEADLESS_MODE"),
            recovery_enabled=env_bool("AUTOMATO_RECOVERY_ENABLED",
                                      config.RECOVERY_ENABLED),
            challenge_check=env_bool("AUTOMATO_CHALLENGE_CHECK",
                                     config.CHALLENGE_CHECK),
            anti_automation=env_bool("AUTOMATO_ANTI_AUTOMATION",
                                     config.ANTI_AUTOMATION),
            llm_provider=os.environ.get(
                "AUTOMATO_LLM_PROVIDER",
                getattr(config, "LLM_PROVIDER", "ai_studio")),
        )

    @classmethod
    def from_args(cls, args) -> "RunSettings":
        """Apply CLI flags on top of defaults() (highest precedence)."""
        s = cls.defaults()
        if getattr(args, "visibility", None):
            s.visibility = _require(args.visibility, _VISIBILITIES, "visibility")
        if getattr(args, "tts", None):
            s.tts_provider = _require(args.tts, _TTS_PROVIDERS, "tts_provider")
        if getattr(args, "browser", None):
            s.browser_choice = _require(args.browser, _BROWSERS, "browser_choice")
        if getattr(args, "headless_mode", None):
            s.headless_mode = _require(args.headless_mode, _HEADLESS_MODES,
                                       "headless_mode")
        # Backwards-compatible flag: --headless forces full headless.
        if getattr(args, "headless", False):
            s.headless_mode = "full"
        return s

    @classmethod
    def build(cls, args=None) -> "RunSettings":
        """Build once per run: defaults <- env vars <- CLI flags."""
        return cls.from_args(args) if args is not None else cls.defaults()

    # -- convenience -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "visibility": self.visibility,
            "tts_provider": self.tts_provider,
            "browser_choice": self.browser_choice,
            "headless_mode": self.headless_mode,
            "recovery_enabled": self.recovery_enabled,
            "challenge_check": self.challenge_check,
            "anti_automation": self.anti_automation,
            "llm_provider": self.llm_provider,
            "force_unlisted": self.force_unlisted,
        }

    def __repr__(self) -> str:  # concise, for logs
        return "RunSettings(" + ", ".join(
            f"{k}={v!r}" for k, v in self.to_dict().items()) + ")"
