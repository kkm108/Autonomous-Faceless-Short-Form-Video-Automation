"""R7: brand-channel routing — allowlist validation, topic->channel mapping,
safe main-channel default, URL-based active-channel reading, and the hard
mismatch abort."""
import pytest

import automato.config as config
from automato import channels
from automato.settings import RunSettings, SettingsError


def test_whitelist_contains_known_channels():
    wl = channels.channel_whitelist()
    assert "main" in wl
    assert channels.channel_id_for("main") == "UCSH1A5BGmsdNq1oqS1HHLpg"
    assert channels.channel_id_for("es-finance") == "UCf0SpoFyFHpegpgBGyRK77w"


def test_registry_loader_snapshots(tmp_path, monkeypatch):
    reg = tmp_path / "channels.yaml"
    reg.write_text(
        'default_channel: brand-a\nchannels:\n'
        '  brand-a:\n    channel_id: "AAAAAAAAAAAAAAAAAAAAAAAA"\n'
        '    language: es\n    keywords: [dinero, ahorro]\n'
        '    branding:\n      accent_color: "#112233"\n',
        encoding="utf-8")
    monkeypatch.setattr(config, "CHANNELS_FILE", reg)
    data = config._load_channel_registry()
    assert data["default_channel"] == "brand-a"
    assert data["channels"]["brand-a"]["language"] == "es"
    assert config.CHANNELS_FILE == reg


def test_invalid_registry_channel_id_fails_loudly(tmp_path, monkeypatch):
    reg = tmp_path / "channels.yaml"
    reg.write_text(
        'default_channel: main\nchannels:\n'
        '  main:\n    channel_id: "not-24-chars"\n',
        encoding="utf-8")
    monkeypatch.setattr(config, "CHANNELS_FILE", reg)
    with pytest.raises(ValueError):
        config._load_channel_registry()


def test_invalid_whitelist_id_raises(monkeypatch):
    monkeypatch.setenv("AUTOMATO_CHANNEL_WHITELIST",
                       '{"main": "not-a-real-id"}')
    with pytest.raises(SettingsError):
        channels.channel_whitelist()


def test_unknown_channel_raises():
    with pytest.raises(SettingsError):
        channels.validate_channel_name("nonexistent")
    with pytest.raises(SettingsError):
        channels.resolve_channel_name("anything", explicit="nonexistent")


def test_resolve_defaults_to_safe_main_channel():
    assert channels.resolve_channel_name("") == "main"
    assert channels.resolve_channel_name(None) == "main"
    assert channels.resolve_channel_name("how birds fly") == "main"


def test_resolve_keyword_map_is_case_insensitive():
    assert channels.resolve_channel_name("The future of AI agents") == "main"
    assert channels.resolve_channel_name("ai agents") == "main"
    assert channels.resolve_channel_name("PERSONAL FINANCE tips") == "es-finance"
    assert channels.resolve_channel_name("rent vs buy REAL ESTATE") == "es-finance"


def test_resolve_first_keyword_hit_wins():
    # R8-A4: first-listed-wins is now DETERMINISTIC — keywords are scanned in
    # registry order (channels.yaml), so a topic matching both channels'
    # keywords resolves to whichever channel appears first in the registry
    # (main, then es-finance), never to iteration order.
    got = channels.resolve_channel_name("ai finance investing money")
    assert got == "main"


def test_resolve_profile_lookups_from_registry():
    # R8-A1/A2: the same registry entry drives routing, scripting language and
    # TTS voice; lookups for unknown channels degrade to safe defaults.
    assert channels.language_for("main") == "en"
    assert channels.language_for("es-finance") == "es"
    assert channels.language_for("ghost") == "en"
    assert channels.tone_for("main")
    brand = channels.branding_for("es-finance")
    assert brand.get("accent_color") == "#ffd9a0"
    assert channels.profile_for("main")["channel_id"] == "UCSH1A5BGmsdNq1oqS1HHLpg"
    assert channels.resolve_language("main", "finance news") == "en"
    assert channels.resolve_language("es-finance", "finance news") == "es"


def test_explicit_channel_wins_over_keywords():
    assert channels.resolve_channel_name(
        "finance money", explicit="main") == "main"
    assert channels.resolve_channel_name(
        "ai agents", explicit="es-finance") == "es-finance"


def test_active_channel_id_from_studio_url():
    class Page:
        url = ("https://studio.youtube.com/channel/"
               "UCSH1A5BGmsdNq1oqS1HHLpg/analytics?x=1")

    assert channels.active_channel_id(Page()) == "UCSH1A5BGmsdNq1oqS1HHLpg"


def test_active_channel_id_missing_returns_none():
    class Page:
        url = "https://studio.youtube.com/videos"

    assert channels.active_channel_id(Page()) is None
    assert channels.active_channel_id(None) is None


class _Page:
    def __init__(self, url):
        self.url = url
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url


def test_ensure_channel_routes_and_verifies():
    page = _Page("https://studio.youtube.com/channel/UCf0SpoFyFHpegpgBGyRK77w")
    active = channels.ensure_channel(page, config.CHANNEL_WHITELIST["main"])
    assert active == config.CHANNEL_WHITELIST["main"]
    assert page.goto_calls == [
        "https://studio.youtube.com/channel/UCSH1A5BGmsdNq1oqS1HHLpg"]


def test_ensure_channel_noop_when_already_active():
    page = _Page("https://studio.youtube.com/channel/UCSH1A5BGmsdNq1oqS1HHLpg")
    active = channels.ensure_channel(
        page, config.CHANNEL_WHITELIST["main"])
    assert active == "UCSH1A5BGmsdNq1oqS1HHLpg"
    assert page.goto_calls == []


def test_require_channel_aborts_on_mismatch():
    from automato.adapters.base import ExecutorError

    page = _Page("https://studio.youtube.com/channel/UCf0SpoFyFHpegpgBGyRK77w")
    with pytest.raises(ExecutorError):
        channels.require_channel(
            page, config.CHANNEL_WHITELIST["main"], allow_routing=False)
    # allow_routing routes first, then re-verifies -> success is possible.
    page2 = _Page("https://studio.youtube.com/channel/UCf0SpoFyFHpegpgBGyRK77w")
    assert channels.require_channel(
        page2, config.CHANNEL_WHITELIST["main"]) == config.CHANNEL_WHITELIST["main"]


def test_run_settings_channel_validation():
    with pytest.raises(SettingsError):
        RunSettings(channel_name="not-a-channel")
    s = RunSettings(channel_name="main")
    assert s.channel_name == "main"
    assert RunSettings.defaults().publish_confirm is True
    assert RunSettings.defaults().topic_ideation_enabled is True


def test_run_settings_env_overrides(monkeypatch):
    monkeypatch.setenv("AUTOMATO_PUBLISH_CONFIRM", "false")
    assert RunSettings.defaults().publish_confirm is False
    monkeypatch.setenv("AUTOMATO_TOPIC_IDEATION_ENABLED", "0")
    assert RunSettings.defaults().topic_ideation_enabled is False
    monkeypatch.setenv("AUTOMATO_CHANNEL_WHITELIST",
                       '{"brand-a": "AAAAAAAAAAAAAAAAAAAAAAAA"}')
    assert channels.channel_id_for("brand-a") == "AAAAAAAAAAAAAAAAAAAAAAAA"
    assert channels.channel_id_for("main") is None
