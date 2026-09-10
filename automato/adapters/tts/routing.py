"""Language-aware TTS routing (R6 + R8-A2).

Single source of truth for which primary provider serves which language. The
existing local offline chain (soundtools -> edge_tts -> pyttsx3) is the universal
fallback; a language may add an online primary site ahead of it.

R8-A2: the *channel registry*'s ``language`` field is now the single fact that
drives the scripting language, the publish channel choice AND the TTS route.
``build_tts_chain`` accepts that language explicitly so the three consumers can
never silently disagree. The text-detection fallback (Devanagari -> `sa`) still
applies only when no channel-driven language is available.

Route rules (honest, not universal): the browser tool (SoundTools) and the
offline pyttsx3 engine are English-oriented, so for known non-English languages
the chain skips ahead to edge-tts (whose neural voices genuinely cover es/hi) and
ends with pyttsx3 solely as a last-resort. English keeps the full local chain.
"""
from __future__ import annotations

import re

# The guaranteed offline chain, tried in order after any language primary fails.
LOCAL_TTS_CHAIN = ["soundtools", "edge_tts", "pyttsx3"]
# Languages whose primary route is edge-tts (browser tool + pyttsx3 are
# English-oriented; edge-tts has genuine neural voices for these).
EDGE_FIRST_LANGUAGES = ("es", "hi")
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


def build_tts_chain(text: str, forced_provider: "str | None" = None,
                    language: "str | None" = None) -> list:
    """Ordered provider chain for ``text``.

    ``forced_provider`` (from settings/TTS_PROVIDER) wins outright; "auto"/None
    routes by language: an explicit channel-driven ``language`` (R8-A2) takes
    precedence over text detection; a language with an enabled primary site gets
    that site ahead of the local chain, otherwise the local chain.
    """
    provider = (forced_provider or "auto").strip().lower()
    if provider not in ("", "auto"):
        return [provider]

    from ... import config

    lang = (language or "").strip().lower() or detect_language(text)
    primary = PRIMARY_TTS_BY_LANGUAGE.get(lang)
    if primary == "vagdhenu" and config.VAGDHENU_ENABLED:
        return ["vagdhenu", *LOCAL_TTS_CHAIN]
    if lang in EDGE_FIRST_LANGUAGES:
        # Non-English narration is best served by edge-tts' neural voices; skip
        # the English-oriented browser tool, keep pyttsx3 as a last resort.
        return ["edge_tts", "pyttsx3"]
    return list(LOCAL_TTS_CHAIN)


def edge_voice_for(language: "str | None") -> str:
    """The edge-tts neural voice for a channel registry language (R8-A2)."""
    from ... import config

    lang = (language or "").strip().lower()
    return config.EDGE_TTS_VOICES.get(lang, config.EDGE_TTS_VOICE)
