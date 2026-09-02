"""Generic LLM scripting adapter (browser-driven, multi-provider).

Fully browser-automated. Two free providers so the pipeline runs without
interruption:

  * ``ai_studio`` (user's preferred provider) — Google AI Studio web UI (needs a
    signed-in Google session; auto-falls back if not logged in).
  * ``duckai`` (duck.ai) — DuckDuckGo AI Chat, free, **no signup required**, so the
    scripting stage completes autonomously without any account.

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

from ...llm import chat as browser_chat
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
def _duckai_ask(page, topic: str) -> Optional[dict]:
    prompt_text = f"{SYS_PREAMBLE}\n\n{build_user_prompt(topic)}"
    reply = browser_chat.ask(page, prompt_text)
    return _parse_script(reply, topic)


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
    box = locs.try_resolve(page, "prompt_box", timeout=12000)
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

    last = ""
    deadline = time.time() + 200
    while time.time() < deadline:
        time.sleep(3)
        cur = ""
        for sel in AI_STUDIO_LOCS["model_message"]:
            try:
                loc = page.locator(sel).last
                if loc.count() > 0:
                    cur = loc.inner_text(timeout=3000) or ""
                    break
            except Exception:  # noqa: BLE001
                continue
        if cur:
            last = cur
        parsed = _parse_script(last, "")
        if parsed is not None and "END" in last.upper():
            return parsed
    return _parse_script(last, "")


# ---------------------------------------------------------------------------
# adapter entry --------------------------------------------------------------
# ---------------------------------------------------------------------------
def run(ctx, inputs, run_dir, session):
    topic = str(inputs.get("topic", "")).strip()
    if not topic:
        raise ValueError("'topic' input is required for the scripting stage")

    page = session.first_page()
    from ...resilience.interaction import ElementInteractor
    from ...resilience.location import ProviderLocations

    ux = ElementInteractor(page)
    locs = ProviderLocations(AI_STUDIO_LOCS)
    preferred = getattr(ctx.global_config, "LLM_PROVIDER", "ai_studio")

    script = None
    if preferred == "ai_studio":
        try:
            script = _ai_studio_ask(page, ux, locs,
                                    f"{SYS_PREAMBLE}\n\n{build_user_prompt(topic)}")
        except Exception as exc:  # noqa: BLE001
            log.warning("AI Studio scripting failed (%s); trying duck.ai", exc)

    if script is None:
        # duck.ai with retries (fresh chat each attempt)
        for attempt in (1, 2, 3):
            try:
                script = _duckai_ask(page, topic)
            except Exception as exc:  # noqa: BLE001
                log.warning("Duck.ai attempt %d failed (%s)", attempt, exc)
                script = None
            if script is not None:
                break
            log.warning("Duck.ai attempt %d produced no parseable script; retrying", attempt)
            time.sleep(5)

    if script is None:
        raise RuntimeError("Failed to obtain a script from the LLM")

    out = run_dir / "script.json"
    out.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Script generated: %r (%d captions, %d image prompts)",
             script["title"], len(script["captions"]), len(script["image_prompts"]))
    return {"script": str(out)}
