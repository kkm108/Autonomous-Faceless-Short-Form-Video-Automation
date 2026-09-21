"""Weighted, pinnable provider selection (A/B-test layer over the fallback cascade).

Every stage (TTS, assets, scripting) has an *ordered* failure chain today: the
first provider is the primary, and when it raises, the remaining providers run in
the current safety order. This module adds a *layer on top of* that cascade:

* ``AUTOMATO_<STAGE>_WEIGHTS`` (e.g. ``AUTOMATO_TTS_WEIGHTS="edge_tts:4,soundtools:1,
  pyttsx3:1"``) decides which provider is tried FIRST via a weighted random roll.
  The roll never removes a provider or reorders the rest of the chain — a bad roll
  leads with a fallback, and if it fails, the cascade continues EXACTLY in today's
  order from there.
* ``AUTOMATO_<STAGE>_PIN`` forces a *lead* provider deterministically (bypassing
  the weight roll entirely) while keeping the rest of the chain as the fallback
  safety net.
* ``AUTOMATO_<STAGE>_AB_TEST=1`` opts a stage into experiment mode: a provider
  that was *chosen by the weight roll* (not reached by failure) publishes with its
  normal intended visibility instead of being force-downgraded to unlisted. It is
  only eligible on ``low``-risk-tier channels — a high-tier channel forced into
  health-exercise semantics regardless of the env setting (the fact-check pass
  exists exactly to stop unreviewed risk on channels where being wrong causes
  harm). The provider-choice origin is always recorded to a per-run
  ``provider_choices.json`` so a reviewer can tell "failed over" from "experiment
  picked it".

Environment is re-read per call (matching the ``channels.py`` pattern) so a change
applies without restart and tests can flip a stage's config independently.
Stage names driving the env prefix: ``tts``, ``assets``, ``llm``.
"""
from __future__ import annotations

import json
import logging
import os
import random
from pathlib import Path

from . import config
from .settings import SettingsError

log = logging.getLogger(__name__)

# Providers with a weight of 0 are valid chain members (they still get tried as a
# failure fallback) but are never rolled to lead the run. Constants' env overrides
# are read by weights_for()/pinned_for()/ab_mode_for() at each call.
DEFAULT_WEIGHTS = {
    "tts": {"edge_tts": 4, "soundtools": 1, "pyttsx3": 1},
    "assets": {"perchance": 4, "pollinations": 1},
    "llm": {"ai_studio": 4, "duckai": 1, "ask_brave": 1, "gemini": 0, "chatgpt": 0},
    # R11-F3: per-slide Ken Burns motion style (see assembly/ffmpeg MOTION_PRESETS).
    "motion": {"zoom_in": 1, "zoom_out": 1, "pan_left": 1, "pan_right": 1, "drift": 1},
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise SettingsError(f"Invalid {name}={raw!r}; expected a boolean.")


def parse_weights(spec: str) -> dict:
    """Parse ``"edge_tts:4,soundtools:1"`` into an int-weight dict.

    Raises ``SettingsError`` for empty/garbage input or non-positive integer
    weights, so a typo in an env override fails at selection time with a clear
    message rather than silently keeping the previous default.
    """
    if not spec or not str(spec).strip():
        raise SettingsError(f"weights spec is empty: {spec!r}")
    try:
        pairs = [part.strip() for part in str(spec).split(",") if part.strip()]
        weights: dict = {}
        for part in pairs:
            if ":" not in part:
                raise SettingsError(
                    f"weights entry {part!r} must look like 'provider:weight'")
            name, _, num = part.partition(":")
            name = name.strip()
            value = int(num.strip())
            if value < 0:
                raise SettingsError(f"weights entry {part!r} must be >= 0")
            weights[name] = value
    except ValueError as exc:
        raise SettingsError(f"weights spec {spec!r} has a non-integer weight: {exc}")
    if not weights:
        raise SettingsError(f"weights spec {spec!r} produced no entries")
    return weights


def weights_for(stage: str) -> dict:
    """Effective weight table for a stage: env override or the code default."""
    raw = os.environ.get(f"AUTOMATO_{stage.upper()}_WEIGHTS", "").strip()
    if raw:
        return parse_weights(raw)
    return dict(config.DEFAULT_WEIGHTS.get(stage, {}) or {})


def pinned_for(stage: str) -> "str | None":
    """The env/configured pin for a stage (''/None = no pin)."""
    raw = (os.environ.get(f"AUTOMATO_{stage.upper()}_PIN") or "").strip()
    return raw or None


def ab_mode_for(stage: str) -> bool:
    """Whether the stage is opted into A/B-test mode (env override wins)."""
    return _env_bool(f"AUTOMATO_{stage.upper()}_AB_TEST",
                     bool(getattr(config, f"{stage.upper()}_AB_TEST", False)))


def ab_enabled_for(stage: str, channel_name: "str | None") -> bool:
    """Whether A/B-test mode is *eligible* for this run's channel.

    The env setting is insufficient on its own: a ``high``-risk-tier channel
    always forces health-exercise semantics (weighted selection still runs, but
    any fallback use degrades + downgrades to unlisted) regardless of the
    ``AUTOMATO_*_AB_TEST`` value. Testing whether a cheaper provider is "good
    enough" by serving it unreviewed to a finance/health audience is exactly the
    risk the fact-check pass exists to prevent.
    """
    if not ab_mode_for(stage):
        return False
    if channel_name:
        from .channels import risk_tier_for

        if risk_tier_for(channel_name) == "high":
            return False
    return True


def _weighted_pick(weights: dict, chain: list, rng) -> "str | None":
    weighted = [(p, weights[p]) for p in chain
                if p in weights and weights[p] > 0]
    if not weighted:
        return None
    total = sum(w for _, w in weighted)
    if total <= 0:
        return None
    r = rng.random() * total
    for p, w in weighted:
        r -= w
        if r < 0:
            return p
    return weighted[-1][0]


def lead_with(stage: str, chain: list, rng=None):
    """Return ``(rotated_chain, head, chosen_by)`` for a stage's ordered chain.

    ``chosen_by`` answers the "who decided the lead" question a reviewer needs:
      * ``pinned``   -- AUTOMATO_<STAGE>_PIN forced it (weights bypassed)
      * ``weight``   -- the weighted random roll picked it
      * ``default``  -- no pin, no usable weights, or a single-provider chain
                        (deterministic: today's primary)
    A *forced provider* (e.g. ``TTS_PROVIDER=soundtools``) is a single-provider
    chain and therefore surfaces as ``default`` here; callers that want the
    distinct ``forced`` label short-circuit weights entirely (see the adapters).

    The rotation only moves the picked head to the front and keeps every other
    member in the chain's ORIGINAL relative order, and never moves an unweighted
    leader (e.g. ``vagdhenu`` ahead of the local chain) behind a rolled provider.
    So the failure cascade after the lead is exactly today's safety order.
    """
    chain = list(chain)
    if not chain:
        raise SettingsError(f"cannot lead an empty provider chain for stage {stage!r}")
    pin = pinned_for(stage)
    if pin:
        if pin not in chain:
            log.warning("Pin %r for stage %s not in chain %s; ignoring pin",
                        pin, stage, chain)
        else:
            rest = [p for p in chain if p != pin]
            return [pin, *rest], pin, "pinned"
    if len(chain) == 1:
        return chain, chain[0], "default"
    weights = weights_for(stage)
    picked = _weighted_pick(weights, chain, rng or random)
    if picked is None:
        return chain, chain[0], "default"
    lead = [p for p in chain if p not in weights]
    rest = [p for p in chain if p in weights and p != picked]
    return [*lead, picked, *rest], picked, "weight"


def allow_ab_exception(stage: str, channel_name: "str | None", chosen_by: str,
                       picked: str, used: str, failed_any: bool) -> bool:
    """Whether a fallback provider may skip the degraded downgrade.

    True only in A/B-test mode when the used provider was *chosen by the weight
    roll itself* (it did not fail, and nothing before it failed): that is an
    experiment arm publishing at its normal visibility, not a failure fallback.
    Any actual failure path degrades in both modes.
    """
    return (
        chosen_by == "weight"
        and used == picked
        and not failed_any
        and ab_enabled_for(stage, channel_name)
    )


def decide_degraded(stage: str, channel_name: "str | None", chosen_by: str,
                    picked: str, used: str, failed_any: bool,
                    primary: str) -> tuple:
    """The degraded-reason decision for a used provider.

    Returns ``(degraded_reason, excluded_from_degradation)``:
      * used == the chain's primary        -> (None, False)
      * used is a fallback reached BY FAILURE (or in health mode) -> (reason, False)
      * used is a fallback the weight roll picked in A/B mode      -> (None, True)

    ``failed_any`` means any provider in the chain failed before ``used`` was
    tried (so even a weight-picked fallback that cascaded after a failure is a
    failure fallback, not an experiment arm).
    """
    if used == primary:
        return None, False
    if allow_ab_exception(stage, channel_name, chosen_by, picked, used, failed_any):
        return None, True
    reason = (f"provider '{used}' served the stage after {chosen_by} selection "
              f"(primary is '{primary}')")
    return reason, False


def record_choice(run_dir, stage: str, info: dict) -> Path:
    """Append one stage's provider decision to the run's provider_choices.json.

    Written for every provideric stage regardless of mode, so the correlation
    step can always tell "used the fallback because of a failure" apart from
    "used the fallback because the experiment picked it".
    """
    path = Path(run_dir) / "provider_choices.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        log.warning("Overwriting unreadable provider_choices.json in %s", run_dir)
        data = {}
    stages = dict(data.get("stages", {}) or {})
    stages[stage] = info
    payload = {"stages": stages}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path
