"""Monthly/manual provider *selector health check* (R3-F1).

A scheduled ``python -m automato health-check`` re-validates each provider's
*static* locators against the live site in a real browser, independent of a
content run, so UI drift is caught on a schedule instead of by surprise
mid-pipeline. Learned overlays are deliberately excluded: the point is to prove
the maintained static list still matches, not to lean on recovery patches.

Providers without a static locator table (perchance, which scans DOM containers
under the generator frame) get a lightweight structural probe instead.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .settings import RunSettings


def _probe_group(page, probe: Dict, group: str, kind: str,
                 timeout_ms: int) -> tuple:
    from .resilience.location import ProviderLocations

    # No provider name: learned overlay is intentionally not consulted.
    locs = ProviderLocations(probe["locs"])
    try:
        if kind == "hidden":
            locs.resolve_hidden(page, group, timeout=timeout_ms)
        elif kind == "multi":
            locs.resolve_multi(page, group, timeout=timeout_ms)
        else:
            locs.resolve(page, group, timeout=timeout_ms)
        return True, "resolved"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc).splitlines()[0][:160]


def _perchance_probe(page, timeout_ms: int = 15000) -> tuple:
    """Structural probe: the generator runs in an embed iframe under the page."""
    try:
        page.locator("iframe").first.wait_for(state="attached", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        return False, f"no iframe attached: {type(exc).__name__}"
    for f in page.frames:
        if "ai-text-to-image-generator" in (f.url or ""):
            return True, "generator frame present"
    return False, "iframe attached but generator frame not referenced"


def _make_probes():
    from .adapters.scripting import generic_llm
    from .adapters.publish import youtube_studio
    from .adapters.tts import kokoro_tts

    return [
        {
            "name": "youtube",
            "session": "youtube",
            "url": youtube_studio.STUDIO_URL,
            "locs": youtube_studio.LOCS,
            "groups": [
                ("create", "resolve"),
                ("upload_item", "resolve"),
                ("file_input", "hidden"),
            ],
        },
        {
            "name": "ai_studio",
            "session": "ai_studio",
            "url": generic_llm.AI_STUDIO_URL,
            "locs": generic_llm.AI_STUDIO_LOCS,
            "groups": [
                ("prompt_box", "resolve"),
                ("run_button", "resolve"),
                ("model_message", "resolve"),
            ],
        },
        {
            "name": "tts",
            "session": "tts",
            "url": kokoro_tts.TTS_URL,
            "locs": kokoro_tts.LOCS,
            "groups": [
                ("input", "resolve"),
                ("generate", "resolve"),
                ("download_wav", "multi"),
            ],
        },
        {
            "name": "perchance",
            "session": "perchance",
            "url": "https://perchance.org/ai-text-to-image-generator",
            "locs": {},
            "groups": [("generator_frame", "probe")],
        },
    ]


def run(provider: Optional[str] = None, headless: bool = False,
        browser_choice: Optional[str] = None,
        timeout_ms: int = 8000) -> List[dict]:
    """Re-validate each provider's locators live; return a result list.

    Each result: ``{"provider", "url", "group", "ok", "detail"}``. A ``url`` is
    reported as ``"<needs login>"`` when navigation lands outside the expected
    host (e.g. YouTube Studio redirecting to the sign-in page).
    """
    from . import config
    from .browser import session as session_mod
    from .providers import register_all

    register_all()
    config.ensure_dirs()
    settings = RunSettings(
        browser_choice=browser_choice,
        headless_mode="full" if headless else "headed",
        anti_automation=True,
    )
    probes = _make_probes()
    if provider is not None:
        probes = [p for p in probes if p["name"] == provider]
        if not probes:
            raise ValueError(
                f"Unknown provider '{provider}'. Known: "
                + ", ".join(p["name"] for p in _make_probes()))

    results: List[dict] = []
    for probe in probes:
        browser, context = session_mod.open_session(probe["session"],
                                                    settings=settings)
        try:
            page = browser.first_page()
            url = probe["url"]
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:  # noqa: BLE001
                pass
            if probe["groups"] == [("generator_frame", "probe")]:
                ok, detail = _perchance_probe(page)
                results.append({
                    "provider": probe["name"], "url": url,
                    "group": "generator_frame", "ok": ok, "detail": detail,
                })
                continue
            for group, kind in probe["groups"]:
                ok, detail = _probe_group(page, probe, group, kind, timeout_ms)
                results.append({
                    "provider": probe["name"], "url": url,
                    "group": group, "ok": ok, "detail": detail,
                })
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
            session_mod.release_session_lock(probe["session"])
    return results