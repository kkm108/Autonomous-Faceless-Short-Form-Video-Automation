"""R6: pure-logic tests for language-aware TTS routing."""

from automato import config
from automato.adapters.tts import routing

LOCAL = ["soundtools", "edge_tts", "pyttsx3"]


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
