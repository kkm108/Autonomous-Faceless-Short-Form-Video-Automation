"""Reusable browser-driven LLM chat primitive (duck.ai, no login).

Extracted from the scripting adapter so other stages (notably the recovery
agent) can get raw model text — and optionally attach an image (screenshot) —
through the same persistent-browser flow without re-implementing the fragile
duck.ai interaction. Non-image text replies degrade gracefully to a well-formed
reply even when the model can't see the attachment.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

DUCKAI_URL = "https://duck.ai/chat"
DUCKAI_PROMPT_SELECTOR = "textarea[aria-label='Ask anything privately']"

# If the model supports image input, we attach via this file-drop control.
DROP_TARGETS = [
    "div[aria-label*='Drop your photos']",
    "div[aria-label*='Drop your files']",
    "textarea[aria-label='Ask anything privately']",
    "body",
]


def _fresh_chat(page) -> None:
    """Reset to a clean conversation to avoid prior-history noise."""
    try:
        new_chat = page.get_by_role("button", name="New Chat").first
        if new_chat.count() > 0:
            new_chat.click(timeout=4000)
            time.sleep(2)
    except Exception:  # noqa: BLE001
        pass


def _attach_image(page, image_path: Optional[str]) -> None:
    if not image_path:
        return
    for sel in DROP_TARGETS:
        try:
            loc = page.locator(sel).first
            loc.set_input_files(image_path, timeout=4000)
            log.info("Attached image %s via %s", image_path, sel)
            time.sleep(1)
            return
        except Exception:  # noqa: BLE001
            continue
    log.info("Could not attach image; continuing with text-only prompt")


def _current_reply_text(page, anchor_len: int) -> str:
    """Return the text appended after the prompt anchor (empty until the reply
    starts)."""
    try:
        full = page.locator("body").inner_text(timeout=6000) or ""
    except Exception:  # noqa: BLE001
        return ""
    return full[anchor_len:] if len(full) > anchor_len else ""


def wait_for_completion(page, anchor_len: int, timeout_s: int = 220,
                        stop_selector: Optional[str] = None) -> str:
    """Shared, robust signal for "has the LLM finished streaming" (R2-W6).

    Unlike the earlier per-adapter heuristics (a naive substring match that could
    end early, vs. waiting on the whole page), the reply is considered complete
    when BOTH of these hold:

      * the reply text has stopped growing across polls (it "settled"), AND
      * any "stop generating" control (if one is known) is no longer present —
        a stronger, UI-level signal that generation actually finished.

    When no stop control is configured we rely on the settle heuristic alone.
    Returns the accumulated reply text (trimmed of trailing whitespace).
    """
    last = ""
    unchanged = 0
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        cur = _current_reply_text(page, anchor_len)
        if cur and cur != last:
            last = cur
            unchanged = 0
        else:
            unchanged += 1
        if len(last) <= 20:
            continue
        if stop_selector:
            try:
                still_generating = page.locator(stop_selector).count() > 0
            except Exception:  # noqa: BLE001
                still_generating = False
            if still_generating:
                continue  # keep waiting; model is still streaming
        if unchanged >= 2:
            break
    return last.strip()


def ask(page, prompt: str, image_path: Optional[str] = None,
        timeout_s: int = 220) -> str:
    """Send ``prompt`` on duck.ai and return the raw assistant reply text.

    ``page`` is an existing Playwright page (from the persistent browser session).
    """
    page.goto(DUCKAI_URL, wait_until="domcontentloaded", timeout=60000)
    _fresh_chat(page)
    _attach_image(page, image_path)

    box = page.locator(DUCKAI_PROMPT_SELECTOR).first
    box.click(timeout=15000)
    box.fill("", timeout=8000)
    try:
        box.fill(prompt, timeout=8000)
    except Exception:  # noqa: BLE001
        # contenteditable/other fallback
        box.press_sequentially(prompt, delay=2)
    # Snapshot before sending; the reply is text appended after this anchor.
    try:
        anchor = page.locator("body").inner_text(timeout=6000) or ""
    except Exception:  # noqa: BLE001
        anchor = ""
    # duck.ai submits via the "Ask" button; Enter is unreliable here.
    try:
        ask_btn = page.get_by_role("button", name="Ask").last
        if ask_btn.count() > 0:
            ask_btn.click(timeout=6000)
        else:
            box.press("Enter")
    except Exception:  # noqa: BLE001
        box.press("Enter")

    return wait_for_completion(page, len(anchor), timeout_s=timeout_s)
