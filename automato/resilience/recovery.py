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

import json
import logging
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .. import config
from ..llm import chat as browser_chat

log = logging.getLogger(__name__)

# Recovery trend ledger (R3-W1): every LLM-recovery attempt is appended to a local
# counter file under output/, keyed per provider per day. A trend that climbs
# without recovery being applied back down is the early-warning sign that a target
# site changed and the maintained static locators need a real update -- not just
# another per-run patch.
_TREND_FILE = "recovery_trend.json"


def _trend_path() -> Path:
    return config.OUTPUT_DIR / _TREND_FILE


def _track_recovery(provider: str, success: bool) -> None:
    """Record one recovery attempt (per provider, per local day)."""
    path = _trend_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
    except Exception:  # noqa: BLE001
        data = {}
    day = date.today().isoformat()
    prov = provider or "unknown"
    entry = data.setdefault(day, {}).setdefault(prov, {"attempts": 0, "succeeded": 0})
    entry["attempts"] += 1
    if success:
        entry["succeeded"] += 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist recovery trend: %s", exc)


def recovery_trend(provider: Optional[str] = None, days: int = 14) -> Dict[str, dict]:
    """Aggregate per-provider recovery attempts over the last ``days`` days.

    Returns ``{provider: {"attempts": int, "succeeded": int}}`` (plus totals under
    the key ``"__totals__"``). Bounded read of the local counter file.
    """
    path = _trend_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {}
        if not isinstance(data, dict):
            return {}
    except Exception:  # noqa: BLE001
        return {}
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    aggregated: Dict[str, dict] = {}
    for day, providers in data.items():
        if not isinstance(providers, dict) or day < cutoff:
            continue
        for prov, entry in providers.items():
            if provider is not None and prov != provider:
                continue
            if not isinstance(entry, dict):
                continue
            agg = aggregated.setdefault(prov, {"attempts": 0, "succeeded": 0})
            agg["attempts"] += int(entry.get("attempts", 0) or 0)
            agg["succeeded"] += int(entry.get("succeeded", 0) or 0)
    for agg in aggregated.values():
        if agg["attempts"] and not agg["succeeded"]:
            agg["success_rate"] = 0.0
        elif agg["attempts"]:
            agg["success_rate"] = agg["succeeded"] / agg["attempts"]
        else:
            agg["success_rate"] = None
    totals = {"attempts": sum(a["attempts"] for a in aggregated.values()),
              "succeeded": sum(a["succeeded"] for a in aggregated.values())}
    aggregated["__totals__"] = totals
    return aggregated


def trend_alert(provider: Optional[str] = None,
                threshold: float = 3.0) -> List[dict]:
    """Flag providers whose recovery *rate* is climbing vs. the preceding window.

    Compares mean attempts/day over the most recent 7 days against the previous 7.
    When the recent rate is >= ``threshold`` x the baseline and at least 3 attempts
    happened recently, the element is worth surfacing as an early-warning signal.
    """
    path = _trend_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {}
        if not isinstance(data, dict):
            return []
    except Exception:  # noqa: BLE001
        return []
    today = date.today()
    per_prov: Dict[str, dict] = {}
    for day, providers in data.items():
        if not isinstance(providers, dict):
            continue
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        if provider is not None:
            providers = {k: v for k, v in providers.items() if k == provider}
        for prov, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            attempts = int(entry.get("attempts", 0) or 0)
            window = "recent" if d >= today - timedelta(days=7) else (
                "baseline" if d >= today - timedelta(days=14) else None)
            if window is None:
                continue
            slot = per_prov.setdefault(prov, {"recent": 0, "baseline": 0})
            slot[window] += attempts
    alerts: List[dict] = []
    for prov, slot in per_prov.items():
        recent, baseline = slot["recent"], slot["baseline"]
        if recent >= 3 and baseline >= 1 and recent / 7 >= threshold * baseline / 7:
            alerts.append({
                "provider": prov,
                "recent_attempts": recent,
                "baseline_attempts": baseline,
                "fold_increase": round(recent / baseline, 1),
                "note": "Recovery is firing several times more often than the "
                        "preceding week; check whether a target site's markup "
                        "changed and update the maintained static locators.",
            })
    return alerts


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
                    group_name: Optional[str] = None, locs=None,
                    enabled: Optional[bool] = None,
                    provider: Optional[str] = None) -> bool:
    """Ask the LLM which clickable element to click, do it, and learn on success.

    Returns True if a candidate was clicked successfully. Does NOT re-raise the
    original failure; callers continue/crash as they see fit.

    ``enabled`` (R2-W2/R2-F2) carries the run's recovery decision from settings;
    when None the config module default applies. ``provider`` (R3-W1) scopes the
    recovery-trend ledger entry.
    """
    if enabled is not None:
        active = enabled
    else:
        active = config.RECOVERY_ENABLED
    if not active:
        return False
    candidates = _enumerate_clickables(page)
    if not candidates:
        log.info("Recovery: no clickable candidates found; nothing to do")
        return False
    snapshot = build_dom_snapshot(page)
    screenshot = capture_screenshot(page)
    log.info("Recovery: attempting LLM repair for '%s' (%d candidates) [provider=%s]",
             description, len(candidates), provider)
    choice = _attempt(page, description, failed_selectors, candidates, snapshot, screenshot)
    if choice is None or choice <= 0 or choice > len(candidates):
        _track_recovery(provider, False)
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
    _track_recovery(provider, ok)
    return ok


def _learn_if_stable(locs, group_name: str, cand: dict, page) -> None:
    """Persist a learned selector only when it is a robust, unique handle."""
    try:
        eid = cand.get("css", "").lstrip("#")
        if eid and not cand["css"].startswith(f"#{eid}"):
            eid = None
        # Accept a valid CSS id token (hyphens allowed -- e.g. "#create-button");
        # stricter than str.isidentifier() so realistic ids aren't dropped.
        if eid and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", eid):
            selector = f"#{eid}"
            if page.locator(selector).count() == 1:
                locs.learn(group_name, selector)
                return
        # Fall back to role+name if we have both (a stable, readable handle).
        if cand.get("role") and cand.get("name"):
            named = page.get_by_role(cand["role"], name=cand["name"], exact=False)
            if named.count() == 1:
                locs.learn(group_name, f"get_by_role:{cand['role']}:{cand['name']}")
    except Exception:  # noqa: BLE001
        pass
