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


@pytest.mark.parametrize("pre,narration", [
    # The substring "end" inside a value/prose must NOT terminate parsing
    # (R2-W6 regression guard, now asserted at the _parse_script level).
    ("TITLE | Trends and how they end soon\n", "Watch."),
    ("just prose saying this trend will end soon\n", "ok"),
])
def test_parse_script_end_substring_does_not_terminate(pre, narration):
    out = generic_llm._parse_script(pre + f"NARRATION | {narration}\n", "T")
    assert out["spoken_script"] == narration
