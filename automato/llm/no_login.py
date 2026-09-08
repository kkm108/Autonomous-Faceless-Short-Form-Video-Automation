"""No-login web chat providers used as scripting fallbacks (R6).

Tried in order after the signed-in AI Studio attempt: duck.ai (existing, via
``automato.llm.chat``), Ask Brave (search.brave.com/ask), Gemini web (guest mode,
base Flash only), and ChatGPT (guest mode, region-gated, single conversation).

All follow the same shared settle heuristic used everywhere else:
snapshot ``body`` text, submit, then wait for the body to grow past that anchor
(``wait_for_completion``). Live-probed 2026-09 against the current UIs:

  * Ask Brave — plain textarea, submit via ``button:has-text('Ask')``, streaming
    with no stop control (settle-only).
  * Gemini — rich editor (``.ql-editor``); sent via the Enter key (the visible
    send button can instead open an overflow menu), base Flash guest mode. The
    reply streams into the same body.
  * ChatGPT — composer ``textarea#mobile-composer-prompt`` / ``#prompt-textarea``
    with ``button[data-composer-submit]`` (same button exposes a
    ``data-stop-label`` while generating). A signed-out bottom sheet may cover the
    composer; we dismiss it before typing. Region-gated last resort.
"""
from __future__ import annotations

import logging
import time

from .. import config
from . import chat as browser_chat

log = logging.getLogger(__name__)

ASK_BRAVE_URL = "https://search.brave.com/ask"
GEMINI_URL = "https://gemini.google.com/app"
CHATGPT_URL = "https://chatgpt.com/"

BRAVE_COMPS = ["textarea", "div[contenteditable='true']"]
BRAVE_SUBMITS = ["button:has-text('Ask')"]

GEMINI_COMPS = ["rich-textarea .ql-editor", "rich-textarea div[contenteditable]"]
GEMINI_SUBMITS = ["button[aria-label='Send message']"]

CHATGPT_DISMISS = [
    "button[aria-label='Back to ChatGPT']",
    "button[data-bottom-sheet-dismiss-button]",
    "button[aria-label='Dismiss']",
]
CHATGPT_COMPS = [
    "textarea#mobile-composer-prompt",
    "textarea.wm-composer-textarea",
    "#prompt-textarea",
    "form textarea",
    "div[contenteditable='true']",
]
CHATGPT_SUBMITS = [
    "button[data-composer-submit]",
    "button[data-send-label='Send message']",
]
CHATGPT_STOP = "button[data-stop-label]"


def _pick(page, selectors):
    """First visible match among the candidate selectors, or ``None``."""
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=6000) or ""
    except Exception:  # noqa: BLE001
        return ""


def _type_into(page, box, prompt: str) -> None:
    """Fill the prompt via the most reliable input path for that element.

    Contenteditable rich editors (Gemini) accept typed keystrokes, not a raw
    value set; plain textareas take ``fill`` directly.
    """
    try:
        ce = box.get_attribute("contenteditable") or ""
    except Exception:  # noqa: BLE001
        ce = ""
    if ce.strip() == "true":
        box.click(force=True, timeout=8000)
        box.press_sequentially(prompt, delay=6)
    else:
        box.fill(prompt, timeout=8000)


def _submit(page, box, submits, mode: str = "button") -> bool:
    """Submit the chat. ``mode="enter"`` (Gemini) uses the Enter key, which is
    its reliable send path; the on-page 'Send message' button can instead open an
    overflow menu. Button mode clicks the first enabled submit and falls back to
    Enter."""
    if mode == "enter":
        try:
            box.press("Enter")
            return True
        except Exception:  # noqa: BLE001
            return False
    for sel in submits:
        btn = page.locator(sel).first
        try:
            if (btn.count() > 0 and btn.is_visible()
                    and btn.get_attribute("aria-disabled") != "true"):
                btn.click(timeout=4000)
                return True
        except Exception:  # noqa: BLE001
            continue
    try:
        box.press("Enter")
        return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _ask(page, url, comps, submits, prompt: str, settle_s: float,
         dismiss=(), stop=None, mode: str = "button") -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(settle_s)
    for sel in dismiss:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=2500)
                time.sleep(1)
                break
        except Exception:  # noqa: BLE001
            continue
    box = _pick(page, comps)
    if box is None:
        raise RuntimeError(f"No compose box found on {url}")
    _type_into(page, box, prompt)
    anchor = len(_body_text(page))
    if not _submit(page, box, submits, mode=mode):
        raise RuntimeError(f"Could not submit prompt on {url}")
    return browser_chat.wait_for_completion(
        page, anchor, timeout_s=config.GENERIC_LLM_POLL_DEADLINE_S,
        stop_selector=stop)


def ask_brave(page, prompt: str) -> str:
    return _ask(page, ASK_BRAVE_URL, BRAVE_COMPS, BRAVE_SUBMITS, prompt,
                settle_s=8)


def ask_gemini(page, prompt: str) -> str:
    # Gemini submits on Enter; the visible send button is unreliable (it can open
    # a "copy prompt" overflow instead of sending) — enter mode (R6).
    return _ask(page, GEMINI_URL, GEMINI_COMPS, GEMINI_SUBMITS, prompt,
                settle_s=9, mode="enter")


def ask_chatgpt(page, prompt: str) -> str:
    return _ask(page, CHATGPT_URL, CHATGPT_COMPS, CHATGPT_SUBMITS, prompt,
                settle_s=8, dismiss=CHATGPT_DISMISS, stop=CHATGPT_STOP)
