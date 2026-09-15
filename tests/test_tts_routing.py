"""R6 + R10: pure-logic tests for language-aware TTS routing and the
edge-tts voice-catalog resolution."""

import json

from automato import config
from automato.adapters.tts import routing
from automato.adapters.tts.kokoro_tts import (
    _edge_voice_names,
    _resolve_edge_voice,
)

# edge-tts is primary; soundtools stays as a service-independent spare; pyttsx3
# is the offline last resort (R10: edge-tts now leads the local chain).
LOCAL = ["edge_tts", "soundtools", "pyttsx3"]


def test_detect_devanagari_is_sanskrit():
    assert routing.detect_language("\u095c\u092f \u0936\u094d\u0930\u0940 \u0930\u093e\u092e") == "sa"


def test_detect_english_is_none():
    assert routing.detect_language("hello world") is None


def test_detect_empty_is_none():
    assert routing.detect_language("") is None


def test_auto_english_stays_on_local_chain(monkeypatch):
    monkeypatch.setattr(config, "VAGDHENU_ENABLED", False)
    assert routing.build_tts_chain("Hello there") == LOCAL


def test_auto_sanskrit_with_vagdhenu_disabled_stays_local(monkeypatch):
    monkeypatch.setattr(config, "VAGDHENU_ENABLED", False)
    assert routing.build_tts_chain("\u091c\u092f \u0936\u094d\u0930\u0940 \u0930\u093e\u092e") == LOCAL


def test_auto_sanskrit_with_vagdhenu_enabled_slots_first(monkeypatch):
    monkeypatch.setattr(config, "VAGDHENU_ENABLED", True)
    chain = routing.build_tts_chain("\u091c\u092f \u0936\u094d\u0930\u0940 \u0930\u093e\u092e")
    assert chain[0] == "vagdhenu"
    assert chain[1:] == LOCAL


def test_forced_provider_wins_over_language(monkeypatch):
    monkeypatch.setattr(config, "VAGDHENU_ENABLED", True)
    assert routing.build_tts_chain(
        "\u091c\u092f \u0936\u094d\u0930\u0940 \u0930\u093e\u092e", "edge_tts") == ["edge_tts"]


def test_routing_table_shape():
    # no unknown languages get silently routed; "sa" is the only special case.
    assert set(routing.PRIMARY_TTS_BY_LANGUAGE) == {"sa"}
    assert routing.PRIMARY_TTS_BY_LANGUAGE["sa"] == "vagdhenu"


def test_local_chain_orders_edge_first():
    assert routing.LOCAL_TTS_CHAIN == ["edge_tts", "soundtools", "pyttsx3"]


def test_edge_voice_names_reads_local_cache(tmp_path, monkeypatch):
    # Prefer the deterministic local cache over edge-tts' live list.
    cache = tmp_path / "voices.json"
    cache.write_text(json.dumps([
        {"ShortName": "en-US-ChristopherNeural", "Locale": "en-US"},
        {"ShortName": "es-ES-AlvaroNeural", "Locale": "es-ES"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config, "EDGE_VOICES_CACHE_FILE", cache)
    monkeypatch.setattr("edge_tts.list_voices", lambda: [
        {"ShortName": "en-US-JennyNeural"}])
    assert _edge_voice_names() == ["en-US-ChristopherNeural", "es-ES-AlvaroNeural"]


def test_edge_voice_names_falls_back_to_live_list(tmp_path, monkeypatch):
    # Unreadable cache -> edge-tts' own list_voices().
    monkeypatch.setattr(config, "EDGE_VOICES_CACHE_FILE", tmp_path / "missing.json")
    monkeypatch.setattr("edge_tts.list_voices", lambda: [
        {"ShortName": "en-US-JennyNeural"},
        {"ShortName": "hi-IN-MadhurNeural"},
    ])
    assert _edge_voice_names() == ["en-US-JennyNeural", "hi-IN-MadhurNeural"]


def test_edge_voice_names_empty_when_everything_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EDGE_VOICES_CACHE_FILE", tmp_path / "missing.json")
    monkeypatch.setattr("edge_tts.list_voices", RuntimeError("no network"))
    assert _edge_voice_names() == []


def test_resolve_edge_voice_keeps_known_voice(tmp_path, monkeypatch):
    cache = tmp_path / "voices.json"
    cache.write_text(json.dumps([
        {"ShortName": "en-US-ChristopherNeural", "Locale": "en-US"}]), encoding="utf-8")
    monkeypatch.setattr(config, "EDGE_VOICES_CACHE_FILE", cache)
    assert _resolve_edge_voice("en", "en-US-ChristopherNeural") == "en-US-ChristopherNeural"


def test_resolve_edge_voice_uses_locale_fallback(tmp_path, monkeypatch):
    # Stale config references a voice no longer listed: pick the first known
    # voice for the SAME locale, preserving en/es/hi fidelity.
    cache = tmp_path / "voices.json"
    cache.write_text(json.dumps([
        {"ShortName": "es-ES-AlvaroNeural", "Locale": "es-ES"},
        {"ShortName": "es-MX-DaliaNeural", "Locale": "es-MX"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config, "EDGE_VOICES_CACHE_FILE", cache)
    assert _resolve_edge_voice("es", "es-ES-EliasNeural") == "es-ES-AlvaroNeural"


def test_resolve_edge_voice_unavailable_catalog_keeps_expected(tmp_path, monkeypatch):
    # Catalog unavailable entirely: skip validation, use configured voice.
    monkeypatch.setattr(config, "EDGE_VOICES_CACHE_FILE", tmp_path / "missing.json")
    monkeypatch.setattr("edge_tts.list_voices", RuntimeError("no network"))
    assert _resolve_edge_voice("en", "en-US-ChristopherNeural") == "en-US-ChristopherNeural"
