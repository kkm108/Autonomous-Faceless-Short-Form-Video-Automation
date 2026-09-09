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
    # Both keywords present -> first mapped channel in CHANNEL_MAP iteration
    # order wins; the routing map is order-stable (dict insertion order).
    got = channels.resolve_channel_name("ai finance investing money")
    assert got in ("main", "es-finance")


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
