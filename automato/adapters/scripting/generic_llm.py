"""Generic LLM scripting adapter (browser-driven, multi-provider).

Fully browser-automated. The user's preferred provider runs first; if it is not
signed in or fails, a chain of free **no-login** web chat UIs is tried in order:
duck.ai -> Ask Brave -> Gemini (guest) -> ChatGPT (guest), so the scripting stage
completes autonomously without any account (R6).

The model is asked for a simple delimited plain-text script (see script_prompts),
which we parse robustly locally. This is far more reliable with small web models
than demanding strict JSON.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from ... import config
from ...llm import chat as browser_chat
from ...llm import no_login
from ...llm.script_prompts import SYS_PREAMBLE, build_user_prompt

log = logging.getLogger(__name__)

AI_STUDIO_URL = "https://aistudio.google.com/prompts/new_chat"

AI_STUDIO_LOCS = {
    "prompt_box": [
        "textarea",
        "rich-textarea",
        ".ql-editor",
        "div[contenteditable='true']",
    ],
    "run_button": [
        "button[aria-label*='Run' i]",
        "button[aria-label*='run' i]",
        "button[aria-label*='Send' i]",
        "button[aria-label*='submit' i]",
        "mat-mdc-fab.primary",
        "button.mat-mdc-fab",
    ],
    "model_message": [
        ".model-response-text",
        ".user-query",
        "markdown-text",
    ],
}

# ---------------------------------------------------------------------------
# duck.ai (no login) ---------------------------------------------------------
# ---------------------------------------------------------------------------
_KEY_RE = re.compile(r"^\s*(TITLE|NARRATION|CAPTION|IMAGE)\s*\|\s*(.*)\s*$")


def _parse_script(text: str, topic: str) -> Optional[dict]:
    """Parse the delimited script format into the standard script dict."""
    title = ""
    narration = ""
    captions: list = []
    images: list = []
    for raw in text.splitlines():
        if raw.strip().upper() == "END":
            break
        m = _KEY_RE.match(raw)
        if not m:
            continue
        key, val = m.group(1).upper(), m.group(2).strip()
        if key == "TITLE" and val:
            title = val
        elif key == "NARRATION" and val:
            narration = (narration + " " + val).strip()
        elif key == "CAPTION" and val:
            captions.append(val)
        elif key == "IMAGE" and val:
            images.append(val)
    if not title:
        title = topic
    if not narration:
        return None
    return {
        "title": title,
        "spoken_script": narration,
        "captions": captions,
        "image_prompts": images,
    }


# ---------------------------------------------------------------------------
# Google AI Studio (login required) -----------------------------------------
# ---------------------------------------------------------------------------
def _ai_studio_ask(page, ux, locs, prompt_text: str) -> Optional[dict]:
    page.goto(AI_STUDIO_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)
    box = locs.try_resolve(page, "prompt_box", timeout=config.GENERIC_LLM_INIT_TIMEOUT_MS)
    if box is None:
        log.info("AI Studio not logged in / no compose box; falling back to duck.ai")
        return None
    # Dismiss any first-run consent/onboarding overlay that intercepts clicks.
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        try:
            page.get_by_role("button", name="Continue").last.click(timeout=1500)
        except Exception:  # noqa: BLE001
            pass
    box.click(force=True, timeout=8000)
    box.fill(prompt_text, timeout=8000)
    run_btn = locs.try_resolve(page, "run_button", timeout=8000)
    if run_btn is not None:
        try:
            run_btn.click(force=True, timeout=8000)
        except Exception:  # noqa: BLE001
            box.press("Enter")
    else:
        box.press("Enter")

    # Snapshot body text before sending; the reply arrives as text appended
    # after this anchor, so the exact same settle-detection as duck.ai applies.
    # Reuses the shared wait_for_completion() instead of a bespoke loop.
    try:
        anchor = page.locator("body").inner_text(timeout=6000) or ""
    except Exception:  # noqa: BLE001
        anchor = ""
    reply = browser_chat.wait_for_completion(
        page, len(anchor), timeout_s=config.GENERIC_LLM_POLL_DEADLINE_S)
    return _parse_script(reply, "")


# ---------------------------------------------------------------------------
# adapter entry --------------------------------------------------------------
# ---------------------------------------------------------------------------
def _provider_sequence(preferred: str) -> list:
    """Ordered provider attempts for scripting: the user's preferred provider
    first, then the configured no-login fallback chain (deduped)."""
    no_login_providers = [p for p in config.LLM_NO_LOGIN_CHAIN if p]
    if preferred == "ai_studio":
        return ["ai_studio", *no_login_providers]
    if preferred in no_login_providers:
        return [preferred, *(p for p in no_login_providers if p != preferred)]
    log.warning("Unknown llm_provider %r; using ai_studio + no-login chain",
                preferred)
    return ["ai_studio", *no_login_providers]


def _ask_no_login(page, provider: str, prompt_text: str) -> Optional[dict]:
    if provider == "duckai":
        reply = browser_chat.ask(page, prompt_text)
    elif provider == "ask_brave":
        reply = no_login.ask_brave(page, prompt_text)
    elif provider == "gemini":
        reply = no_login.ask_gemini(page, prompt_text)
    elif provider == "chatgpt":
        reply = no_login.ask_chatgpt(page, prompt_text)
    else:
        log.warning("Skipping unknown no-login provider %r", provider)
        return None
    if not reply:
        return None
    return _parse_script(reply, "")


def run(ctx, inputs, run_dir, session):
    topic = str(inputs.get("topic", "")).strip()
    if not topic:
        raise ValueError("'topic' input is required for the scripting stage")

    page = session.first_page()
    from ...resilience.interaction import ElementInteractor
    from ...resilience.location import ProviderLocations

    ux = ElementInteractor(page, provider="ai_studio", settings=ctx.settings)
    locs = ProviderLocations(AI_STUDIO_LOCS)
    preferred = ctx.settings.llm_provider or "ai_studio"
    providers = _provider_sequence(preferred)
    prompt_text = f"{SYS_PREAMBLE}\n\n{build_user_prompt(topic)}"

    script = None
    for provider in providers:
        attempts = 3 if provider == "duckai" else 2
        for attempt in range(1, attempts + 1):
            try:
                if provider == "ai_studio":
                    script = _ai_studio_ask(page, ux, locs, prompt_text)
                else:
                    script = _ask_no_login(page, provider, prompt_text)
            except Exception as exc:  # noqa: BLE001
                log.warning("Provider '%s' attempt %d failed (%s)",
                            provider, attempt, exc)
                script = None
            if script is not None:
                log.info("Script obtained via provider '%s'", provider)
                break
            log.warning("Provider '%s' attempt %d produced no parseable script; "
                        "retrying", provider, attempt)
            time.sleep(5)
        if script is not None:
            break

    if script is None:
        raise RuntimeError("Failed to obtain a script from the LLM")

    out = run_dir / "script.json"
    out.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Script generated: %r (%d captions, %d image prompts)",
             script["title"], len(script["captions"]), len(script["image_prompts"]))
    return {"script": str(out)}
