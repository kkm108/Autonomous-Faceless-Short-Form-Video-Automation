"""R2-W2 / R2-F2: typed, validated per-run settings object tests."""
import pytest

from automato.settings import RunSettings, SettingsError


def test_defaults_are_valid():
    s = RunSettings.defaults()
    assert s.visibility in ("public", "unlisted", "private")
    assert s.tts_provider in ("auto", "soundtools", "edge_tts", "pyttsx3")
    assert s.browser_choice in ("edge", "chrome", "brave", "chromium")
    assert s.headless_mode in ("headed", "new", "full")
    assert s.force_unlisted is False


def test_literal_validation_catches_typos_at_build_time():
    with pytest.raises(SettingsError):
        RunSettings(visibility="pulic")
    with pytest.raises(SettingsError):
        RunSettings(tts_provider="auto_")
    with pytest.raises(SettingsError):
        RunSettings(browser_choice="safari")
    with pytest.raises(SettingsError):
        RunSettings(headless_mode="invisible")


def test_force_unlisted_is_mutable_after_build():
    s = RunSettings(visibility="public")
    assert s.force_unlisted is False
    s.force_unlisted = True
    assert s.force_unlisted is True


@pytest.mark.parametrize("field,arg,attr", [
    ("visibility", "public", "visibility"),
    ("tts", "pyttsx3", "tts_provider"),
    ("browser", "brave", "browser_choice"),
    ("headless_mode", "new", "headless_mode"),
])
def test_from_args_applies_cli(field, arg, attr):
    args = type("Args", (), {field: arg})()
    s = RunSettings.from_args(args)
    assert getattr(s, attr) == arg


def test_from_args_headless_flag_forces_full():
    s = RunSettings.from_args(type("Args", (), {"headless": True})())
    assert s.headless_mode == "full"


def test_from_args_ignores_missing_attrs():
    s = RunSettings.from_args(type("Args", (), {}))
    assert s.tts_provider == "auto"


@pytest.mark.parametrize("env_name,env_val,attr,expected", [
    ("AUTOMATO_VISIBILITY", "private", "visibility", "private"),
    ("AUTOMATO_TTS_PROVIDER", "edge_tts", "tts_provider", "edge_tts"),
    ("AUTOMATO_BROWSER", "chrome", "browser_choice", "chrome"),
    ("AUTOMATO_HEADLESS_MODE", "full", "headless_mode", "full"),
])
def test_env_overrides_and_cli_beats_env(monkeypatch, env_name, env_val, attr, expected):
    monkeypatch.setenv(env_name, env_val)
    assert getattr(RunSettings.defaults(), attr) == expected
    # An explicit CLI flag wins over the env var.
    cli_attr = {"visibility": "visibility", "tts_provider": "tts",
                "browser_choice": "browser", "headless_mode": "headless_mode"}[attr]
    win = {"visibility": "public", "tts_provider": "auto",
           "browser_choice": "edge", "headless_mode": "headed"}[attr]
    s = RunSettings.from_args(type("Args", (), {cli_attr: win})())
    assert getattr(s, attr) == win


def test_env_bool_parsing(monkeypatch):
    monkeypatch.setenv("AUTOMATO_RECOVERY_ENABLED", "false")
    assert RunSettings.defaults().recovery_enabled is False
    monkeypatch.setenv("AUTOMATO_ANTI_AUTOMATION", "1")
    assert RunSettings.defaults().anti_automation is True
    monkeypatch.setenv("AUTOMATO_CHALLENGE_CHECK", "off")
    assert RunSettings.defaults().challenge_check is False


def test_env_invalid_value_raises(monkeypatch):
    monkeypatch.setenv("AUTOMATO_VISIBILITY", "everyone")
    with pytest.raises(SettingsError):
        RunSettings.defaults()


def test_to_dict_roundtrip():
    s = RunSettings(visibility="private", tts_provider="soundtools")
    assert s.to_dict()["visibility"] == "private"
    assert s.to_dict()["force_unlisted"] is False


def test_env_overrides_llm_provider(monkeypatch):
    # Default resolves to the no-login chain preferred provider (gemini), since
    # Google gated the free AI Studio Playground. Pinning to a chain entry skips
    # AI Studio start-to-finish, which is how the chain is validated run-to-run.
    assert RunSettings.defaults().llm_provider == "gemini"
    monkeypatch.setenv("AUTOMATO_LLM_PROVIDER", "ask_brave")
    assert RunSettings.defaults().llm_provider == "ask_brave"
    monkeypatch.setenv("AUTOMATO_LLM_PROVIDER", "chatgpt")
    assert RunSettings.defaults().llm_provider == "chatgpt"
