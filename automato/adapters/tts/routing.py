"""Language-aware TTS routing (R6).

Single source of truth for which primary provider serves which language. The
existing local offline chain (soundtools -> edge_tts -> pyttsx3) is the universal
fallback; a language may add an online primary site ahead of it.

Today only classical-verse/chant Sanskrit (`sa`) is special-cased: it may route to
the online vagdhenu (IISc) engine, kept opt-in via config.VAGDHENU_ENABLED because
nor real Sanskrit content exists in the pipeline yet. Everything else (including
all general narration) stays on the local chain. Adding a new site is a one-line
table entry + a provider branch in the TTS adapter.
"""
from __future__ import annotations

import re

# The guaranteed offline chain, tried in order after any language primary fails.
LOCAL_TTS_CHAIN = ["soundtools", "edge_tts", "pyttsx3"]

# language code -> primary online site (opt-in, ahead of the local chain).
PRIMARY_TTS_BY_LANGUAGE = {
    "sa": "vagdhenu",  # classical Sanskrit verse/chant -> IISc online engine
}

# Coarse, dependency-free script detection we actually act on.
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")


def detect_language(text: str) -> "str | None":
    """Coarse language guess from scripts we can route; ``None`` = default."""
    if not text:
        return None
    if _DEVANAGARI.search(text):
        return "sa"
    return None


def build_tts_chain(text: str, forced_provider: "str | None" = None) -> list:
    """Ordered provider chain for ``text``.

    ``forced_provider`` (from settings/TTS_PROVIDER) wins outright; "auto"/None
    routes by language: a language with an enabled primary site gets that site
    ahead of the local chain, otherwise just the local chain.
    """
    provider = (forced_provider or "auto").strip().lower()
    if provider not in ("", "auto"):
        return [provider]

    from ... import config

    primary = PRIMARY_TTS_BY_LANGUAGE.get(detect_language(text))
    if primary == "vagdhenu" and config.VAGDHENU_ENABLED:
        return ["vagdhenu", *LOCAL_TTS_CHAIN]
    return list(LOCAL_TTS_CHAIN)
