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

    anchor_len = len(anchor)
    last = anchor
    unchanged = 0
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        try:
            cur = page.locator("body").inner_text(timeout=6000) or last
        except Exception:  # noqa: BLE001
            cur = last
        if cur != last:
            last = cur
            unchanged = 0
        else:
            unchanged += 1
        reply = last[anchor_len:] if len(last) > anchor_len else ""
        # Heuristic: reply "settles" once it stops growing.
        if len(reply) > 20 and unchanged >= 2:
            break
        if unchanged >= 5:
            break
    reply = last[anchor_len:] if len(last) > anchor_len else ""
    return reply
