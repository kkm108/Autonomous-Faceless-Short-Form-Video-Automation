"""LLM recovery agent: "learn like a human" when a locator fails.

When a brittle step fails, we hand the model (a) a compact DOM snapshot and (b) the
failure details, and ask it to pick from a set of numbered clickable candidates that
*we* enumerate from the live DOM. This avoids asking the model to author CSS
selectors (which small models do poorly — they echo format tokens literally). On
success we learn the winning selector into the provider's overlay so future runs skip
the LLM round-trip.

The chat runs in a *separate tab* so we never navigate the working page away.
"""
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import List, Optional

from ..llm import chat as browser_chat
from .. import config

log = logging.getLogger(__name__)

PROMPT = (
    "A browser automation step failed because it could not find an element to click. "
    "Clickable elements found on the page are listed below, each with a number.\n"
    "Reply with ONLY the number of the element that should be clicked. "
    "If none fits, reply with 0. Reply with a single number and nothing else.\n\n"
    "CLICKABLE ELEMENTS:\n"
)

ANSWER_RE = re.compile(r"^\s*(\d{1,3})\s*$")

# Interactive element types we consider clickable.
_CLICKABLE_SELECTOR = "button, a, [role='button'], [role='link'], input[type='submit'], input[type='button']"


def _visible_name(el) -> str:
    try:
        n = el.get_attribute("aria-label") or el.inner_text(timeout=1500) or ""
        return " ".join(n.split())[:50]
    except Exception:  # noqa: BLE001
        return ""


def _enumerate_clickables(page) -> List[dict]:
    """Return a numbered list of clickable elements with usable selectors."""
    found: List[dict] = []
    seen = set()
    try:
        nodes = page.locator(_CLICKABLE_SELECTOR)
        n = nodes.count()
    except Exception:  # noqa: BLE001
        n = 0
    for i in range(min(n, 40)):
        el = nodes.nth(i)
        try:
            if not el.is_visible(timeout=800):
                continue
        except Exception:  # noqa: BLE001
            continue
        name = _visible_name(el)
        role = None
        for r in ("button", "link", "checkbox"):
            try:
                if el.get_attribute("role") == r and r in ("button", "link"):
                    role = r
                    break
            except Exception:  # noqa: BLE001
                break
        # stable-ish CSS selector fallback
        css = "#"
        try:
            eid = el.get_attribute("id")
            if eid:
                css = f"#{eid}"
            else:
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                css = f"{tag}"
                all_same = page.locator(tag)
                if all_same.count() > 1:
                    css = f"{tag}:nth-of-type({i + 1})"
        except Exception:  # noqa: BLE001
            css = f"*:nth-of-type({i + 1})"
        key = (name, css)
        if key in seen:
            continue
        seen.add(key)
        found.append({"index": len(found) + 1, "name": name, "role": role, "css": css})
    return found


def _parse_answer(reply: str) -> Optional[int]:
    for line in reply.splitlines():
        m = ANSWER_RE.match(line)
        if m:
            return int(m.group(1))
    return None


def _click_candidate(page, cand: dict) -> bool:
    """Click a candidate using its role+name or CSS selector."""
    try:
        if cand.get("role") and cand.get("name"):
            loc = page.get_by_role(cand["role"], name=cand["name"], exact=False).first
            if loc.count() > 0:
                loc.click(timeout=8000)
                return True
        page.locator(cand["css"]).first.click(timeout=8000)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Recovery click candidate failed (%s): %s", cand.get("css"), exc)
        return False


def build_dom_snapshot(page, max_chars: int = 3000) -> str:
    """Compact text snapshot of the page."""
    try:
        text = page.locator("body").inner_text(timeout=4000)
        return (text or "")[:max_chars]
    except Exception:  # noqa: BLE001
        return "(unavailable)"


def capture_screenshot(page) -> Optional[str]:
    """Save a screenshot to a temp file; return its path or None."""
    fd = path = None
    try:
        import os
        fd, path = tempfile.mkstemp(suffix=".png", prefix="recover_")
        os.close(fd)
        fd = None
        page.screenshot(path=path, full_page=False)
        return path
    except Exception as exc:  # noqa: BLE001
        log.warning("Screenshot capture failed: %s", exc)
        if fd is not None:
            try:
                import os
                os.close(fd)
            except Exception:  # noqa: BLE001
                pass
        if path:
            try:
                Path(path).unlink()
            except Exception:  # noqa: BLE001
                pass
        return None


def _attempt(page, description: str, failed_selectors: List[str],
             candidates: List[dict], snapshot: str, screenshot: Optional[str]) -> Optional[int]:
    menu = "\n".join(
        f"{c['index']}. {c['name'] or '<no readable name>'}  [{c['role'] or c['css']}]"
        for c in candidates
    )
    prompt = (
        f"{PROMPT}{menu or '(no clickable elements detected)'}\n\n"
        f"Failed step: {description}\n"
        f"Selectors that failed: {failed_selectors or 'none'}\n"
        f"PAGE TEXT:\n{snapshot}"
    )
    ctx = page.context
    chat_page = ctx.new_page()
    try:
        reply = browser_chat.ask(chat_page, prompt, image_path=screenshot,
                                 timeout_s=config.RECOVERY_DEADLINE_S)
        log.debug("Recovery chat reply (first 200): %r", reply[:200])
        return _parse_answer(reply)
    except Exception as exc:  # noqa: BLE001
        log.warning("Recovery chat failed: %s", exc)
        return None
    finally:
        try:
            chat_page.close()
        except Exception:  # noqa: BLE001
            pass


def attempt_recover(page, description: str, failed_selectors: List[str],
                    group_name: Optional[str] = None, locs=None) -> bool:
    """Ask the LLM which clickable element to click, do it, and learn on success.

    Returns True if a candidate was clicked successfully. Does NOT re-raise the
    original failure; callers continue/crash as they see fit.
    """
    if not config.RECOVERY_ENABLED:
        return False
    candidates = _enumerate_clickables(page)
    if not candidates:
        log.info("Recovery: no clickable candidates found; nothing to do")
        return False
    snapshot = build_dom_snapshot(page)
    screenshot = capture_screenshot(page)
    log.info("Recovery: attempting LLM repair for '%s' (%d candidates)",
             description, len(candidates))
    choice = _attempt(page, description, failed_selectors, candidates, snapshot, screenshot)
    if choice is None or choice <= 0 or choice > len(candidates):
        return False
    cand = candidates[choice - 1]
    ok = _click_candidate(page, cand)
    if ok and locs is not None and group_name:
        # Only learn a selector that (a) is a stable handle and (b) resolves to
        # exactly one element now. This avoids persisting invalid pseudo-selectors
        # (e.g. "get_by_role:...") or fragile nth-of-type ordinals that could
        # target the wrong element on a later run.
        _learn_if_stable(locs, group_name, cand, page)

    if ok:
        log.info("Recovery succeeded on candidate %d (%s)", choice, cand.get("name"))
    return ok


def _learn_if_stable(locs, group_name: str, cand: dict, page) -> None:
    """Persist a learned selector only when it is a robust, unique handle."""
    try:
        eid = cand.get("css", "").lstrip("#")
        if eid and not cand["css"].startswith(f"#{eid}"):
            eid = None
        if eid and eid.isidentifier():
            selector = f"#{eid}"
            if page.locator(selector).count() == 1:
                locs.learn(group_name, selector)
                return
        # Fall back to role+name if we have both (a stable, readable handle).
        if cand.get("role") and cand.get("name"):
            role_sel = f"[role='{cand['role']}']"
            named = page.get_by_role(cand["role"], name=cand["name"], exact=False)
            if named.count() == 1:
                locs.learn(group_name, f"get_by_role:{cand['role']}:{cand['name']}")
    except Exception:  # noqa: BLE001
        pass
    if ok:
        log.info("Recovery succeeded on candidate %d (%s)", choice, cand.get("name"))
    return ok
