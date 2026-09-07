"""R1-F1 / R2-W6: pure-logic tests for the scripting adapter's manifest parser."""
import pytest

from automato.adapters.scripting import generic_llm


def test_parse_script_minimal():
    text = (
        "TITLE | My Video\n"
        "NARRATION | Hello world\n"
        "NARRATION | second line\n"
        "CAPTION | Hello\n"
        "CAPTION | world\n"
        "IMAGE | a sunset\n"
        "IMAGE | a city\n"
        "END\n"
    )
    out = generic_llm._parse_script(text, "Fallback Topic")
    assert out["title"] == "My Video"
    assert out["spoken_script"] == "Hello world second line"
    assert out["captions"] == ["Hello", "world"]
    assert out["image_prompts"] == ["a sunset", "a city"]


def test_parse_script_stops_at_end():
    text = "TITLE | T\nNARRATION | before\nEND\nNARRATION | after\n"
    out = generic_llm._parse_script(text, "Topic")
    assert out["spoken_script"] == "before"


def test_parse_script_uses_topic_as_fallback_title():
    out = generic_llm._parse_script("NARRATION | hi", "The Topic")
    assert out["title"] == "The Topic"


def test_parse_script_returns_none_without_narration():
    assert generic_llm._parse_script("TITLE | only title\n", "T") is None


def test_parse_script_ignores_garbage_lines():
    text = "just some prose\nNARRATION | ok\nCAPTION | c\n"
    out = generic_llm._parse_script(text, "T")
    assert out["spoken_script"] == "ok"
    assert out["captions"] == ["c"]


@pytest.mark.parametrize("text,expected", [
    # Whole-line END delimiter terminates (per the protocol).
    ("TITLE | T\nNARRATION | Intro\nEND\n", True),
    # A bare END line anywhere still counts.
    ("END\nTITLE | T\n", True),
    # The substring "end" inside a word must NOT terminate early (R2-W6).
    ("TITLE | Trends and how they end soon\nNARRATION | Watch.\n", False),
    ("this trend will end soon", False),
    ("", False),
])
def test_is_full_script_delimiter_semantics(text, expected):
    assert generic_llm._is_full_script(text) is expected
