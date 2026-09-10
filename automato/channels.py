"""Brand-channel routing and hard active-channel verification.

The signed-in Google identity carries multiple YouTube channels (a main channel
plus brand channels). Both the Ask Studio ideation pre-stage and the publish
stage are scoped to whatever channel is *active* in Studio, so routing a run to
the wrong channel is a visible mistake. This module centralises:

  * the validated allowlist of known channels + the topic->channel routing map,
    both derived from the external channel registry (``channels.yaml``, R8-A1),
  * resolving a run's target channel (CLI override > topic-keyword map > safe
    main-channel default),
  * per-channel profile lookups (language / tone / branding) driven by that SAME
    registry entry (R8-A2/A3: one fact, three consumers),
  * reading the *active* channel deterministically from the Studio URL's
    ``/channel/<id>`` segment (no account-menu scraping — the URL always carries
    it), and
  * driving Studio onto a chosen channel by routing to its URL.

R7 feature set: config-driven topic->channel mapping, validated allowlist, safe
main-channel default, and a hard verification (abort, never warn) that the
currently active channel matches the intended target before any upload action.
R8: registry-backed profiles, deterministic first-match tie-break (A4).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Dict, List, Optional

from . import config
from .settings import SettingsError

log = logging.getLogger(__name__)

_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{24}$")
# The active channel's id always appears as .../channel/<id> in Studio URLs.
_STUDIO_CHANNEL_RE = re.compile(r"/channel/([A-Za-z0-9_-]{24})")


def channel_whitelist() -> Dict[str, str]:
    """The validated allowlist: {name: channel-id}.

    Re-reads the AUTOMATO_CHANNEL_WHITELIST env override at each call (so a
    change applies without a process restart, and tests can flip it); falls
    back to config's default when unset.
    """
    env = os.environ.get("AUTOMATO_CHANNEL_WHITELIST", "").strip()
    if env:
        try:
            raw = json.loads(env)
        except ValueError as exc:
            raise SettingsError(
                f"AUTOMATO_CHANNEL_WHITELIST is not valid JSON: {exc}"
            ) from exc
    else:
        raw = config.CHANNEL_WHITELIST
    if not isinstance(raw, dict):
        raise SettingsError("AUTOMATO_CHANNEL_WHITELIST must be a JSON object "
                            "mapping channel names to 24-char channel ids.")
    invalid = [
        (name, cid) for name, cid in raw.items()
        if not isinstance(cid, str) or not _CHANNEL_ID_RE.match(cid)
    ]
    if invalid:
        raise SettingsError(
            f"CHANNEL_WHITELIST contains invalid channel ids: {invalid}; a "
            "channel id is exactly 24 alphanumerics/_- (check the "
            "AUTOMATO_CHANNEL_WHITELIST override)."
        )
    return dict(raw)


def _topic_map() -> Dict[str, tuple]:
    """Topic-keyword -> channel routing map (env override re-read per call)."""
    env = os.environ.get("AUTOMATO_CHANNEL_MAP", "").strip()
    if env:
        try:
            parsed = json.loads(env)
        except ValueError as exc:
            raise SettingsError(f"AUTOMATO_CHANNEL_MAP is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SettingsError("AUTOMATO_CHANNEL_MAP must be a JSON object "
                                "mapping channel names to keyword lists.")
        return {name: tuple(kws) for name, kws in parsed.items()}
    return dict(config.CHANNEL_MAP)


def channel_names() -> List[str]:
    return sorted(channel_whitelist())


def channel_id_for(name: str) -> Optional[str]:
    """Channel id for a config name (None for an unknown/unlisted name)."""
    return channel_whitelist().get(name)


def validate_channel_name(name: str) -> str:
    """Ensure ``name`` is in the validated allowlist; raise otherwise."""
    if name not in channel_whitelist():
        raise SettingsError(
            f"Channel '{name}' is not in the validated allowlist. Known "
            f"channels: {', '.join(channel_names())}."
        )
    return name


def resolve_channel_name(topic: str,
                         explicit: Optional[str] = None,
                         default: Optional[str] = None) -> str:
    """Resolve the target channel for a topic.

    Precedence: an explicit CLI override -> the topic-keyword map (first
    keyword hit, case-insensitive) -> the safe main-channel default. The
    result is validated against the allowlist.

    R8-A4: the keyword map is scanned in *registry order* (channels.yaml), so
    when a topic matches several channels the win is deterministic and
    documented: first-listed-wins. ``explicit`` always overrides keywords.
    """
    if explicit:
        return validate_channel_name(explicit)
    if topic:
        lowered = str(topic).lower()
        for name, keywords in _topic_map().items():
            if any(kw in lowered for kw in keywords):
                return validate_channel_name(name)
    return validate_channel_name(default or config.CHANNEL_DEFAULT)


# ---------------------------------------------------------------------------
# R8-A1/A2/A3: per-channel profile lookups from the SAME registry entry that
# drives routing. One entry in channels.yaml -> publish channel + scripting
# language/tone + TTS voice route + caption/thumbnail styling.
# ---------------------------------------------------------------------------

def profile_for(name: str) -> dict:
    """The full registry entry for ``name`` (or {} for an unknown channel)."""
    return dict(config.CHANNEL_PROFILES.get(name) or {})


def language_for(name: str, default: str = "en") -> str:
    """The registry `language` for a channel (drives script + TTS route)."""
    return (profile_for(name).get("language") or default).strip().lower() or default


def branding_for(name: str) -> dict:
    """The registry `branding` block (tone, accent_color, font, logo)."""
    return dict((profile_for(name).get("branding") or {}))


def tone_for(name: str) -> str:
    """Free-form tone guidance injected into the scripting system prompt."""
    return (branding_for(name).get("tone") or "").strip()


def resolve_language(name: Optional[str], topic: str = "") -> str:
    """A2's single language fact: registry language when a channel is known,
    otherwise the coarse text-detection fallback (Sanskrit verse etc.)."""
    if name:
        return language_for(name)
    from .adapters.tts.routing import detect_language
    return detect_language(topic) or "en"


def active_channel_id(page) -> Optional[str]:
    """The channel id that is currently *active* in Studio, read from the URL.

    Studio always carries it as ``.../channel/<id>``; the root redirects to the
    active channel's page. Returns None when the URL does not name a channel
    (e.g. not on Studio / logged out).
    """
    try:
        url = page.url
    except Exception:  # noqa: BLE001
        return None
    if not url:
        return None
    m = _STUDIO_CHANNEL_RE.search(url)
    return m.group(1) if m else None


def ensure_channel(page, target_id: str) -> str:
    """Drive Studio onto ``target_id`` and confirm it stuck.

    Routing Studio to ``/channel/<id>`` switches to that channel in place when
    it belongs to the signed-in Google identity — the way account switching
    actually works (probed). If, after routing, the URL still names a different
    channel, that is a hard failure: raise (never proceed on a mismatch).
    """
    from .adapters.base import ExecutorError

    active = active_channel_id(page)
    if active != target_id:
        log.info("Studio is on channel %s; routing to %s", active, target_id)
        try:
            page.goto(f"https://studio.youtube.com/channel/{target_id}",
                      wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:  # noqa: BLE001
            raise ExecutorError(
                f"Could not route Studio onto channel {target_id}: {exc}",
                retryable=True,
            ) from exc
        time.sleep(config.YOUTUBE_UI_SETTLE_S)
    active = active_channel_id(page)
    if active != target_id:
        raise ExecutorError(
            f"Active Studio channel is {active}, but the intended target is "
            f"{target_id}. Aborting: a wrong-channel publish is a visible "
            "mistake and is never done automatically. Fix the active channel "
            "or re-run with --channel <name>.",
            retryable=False,
        )
    return active


def require_channel(page, target_id: str,
                    allow_routing: bool = True) -> str:
    """Hard pre-upload verification that the active channel is ``target_id``.

    With ``allow_routing`` (default) Studio is routed onto the target first and
    then re-verified. Either way the final gate is the same: if the active
    channel != target, raise (abort), never warn-and-continue.
    """
    if allow_routing:
        return ensure_channel(page, target_id)
    active = active_channel_id(page)
    if active != target_id:
        from .adapters.base import ExecutorError

        raise ExecutorError(
            f"Active Studio channel is {active}, but the run targets "
            f"{target_id}. Aborting before any upload action.",
            retryable=False,
        )
    return active
