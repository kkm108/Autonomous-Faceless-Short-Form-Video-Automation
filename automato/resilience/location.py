"""Centralized, semantic, resilient DOM targeting.

Every provider's UI elements are declared here as a priority-ordered list of
*semantic* locators (ARIA labels, placeholders, roles, visible text). We never use
brittle absolute XPaths. ``resolve`` returns the first locator that matches, and
``resolve_all_roles`` allows fallback across locator groups so minor UI updates
don't break targeting.

Self-learning: when a ``provider`` name is supplied, ``ProviderLocations`` loads a
per-provider *learned overlay* (``profiles/<name>/learned.json``) and tries learned
selectors *before* the static ones. Corrected selectors discovered by the recovery
agent are persisted via ``learn()`` so future runs skip the LLM round-trip.

R3-W2/R3-F2: learned entries are stamped with when/why they were learned, expire
after ``config.LEARNED_SELECTOR_TTL_DAYS``, and are dropped early once the
maintained static selector has resolved the same group successfully
``config.LEARNED_STATIC_HITS_TO_EXPIRE`` times in a row. That expiry/re-validation
guarantees a learned selector cannot shadow a correct static one forever.

R3-W3: when a candidate locator throws a *transient* exception (timeout while
scrolling an otherwise-present element), we retry the same candidate once before
moving on to the next selector, so a flaky network moment can't fall through to a
wrong-but-present fallback match.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union

from .. import config

log = logging.getLogger(__name__)

Locator = Union[str, List[str], Dict[str, List[str]]]


class ProviderLocations:
    """A named bag of semantic locator groups for one provider's UI.

    Usage:
        locs = ProviderLocations({
            "send_button": ["button[aria-label='Send']", "button:has-text('Send')"],
            "textarea": ["textarea, [contenteditable='true']"],
        }, provider="youtube")
        element = locs.resolve(page, "send_button")
    """

    def __init__(self, definitions: Dict[str, Locator], provider: Optional[str] = None,
                 learned_file: Optional[Path] = None):
        self._defs: Dict[str, List[str]] = {}
        for name, value in definitions.items():
            if isinstance(value, str):
                self._defs[name] = [value]
            elif isinstance(value, list):
                self._defs[name] = value
            elif isinstance(value, dict):
                self._defs[name] = value.get("locators", [])

        self.provider = provider
        self._learned_file = learned_file
        # {locator_group: [record, ...]} discovered at runtime. A record is
        # {"selector", "learned_at", "origin", "static_hits_since"}.
        self._learned: Dict[str, List[dict]] = {}
        if provider is not None and learned_file is None:
            self._learned_file = config.PROFILES_DIR / provider / "learned.json"
        self._load_learned()

    # -- learned-overlay helpers -------------------------------------------
    def _load_learned(self) -> None:
        if self._learned_file is None:
            return
        try:
            if self._learned_file.exists():
                data = json.loads(self._learned_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return
                for group, items in data.items():
                    if not isinstance(items, list):
                        continue
                    records: List[dict] = []
                    for item in items:
                        if isinstance(item, str):
                            # pre-R3 file format: bare selector strings.
                            records.append({
                                "selector": item, "learned_at": None,
                                "origin": "legacy", "static_hits_since": 0,
                            })
                        elif isinstance(item, dict) and item.get("selector"):
                            records.append({
                                "selector": str(item["selector"]),
                                "learned_at": item.get("learned_at"),
                                "origin": item.get("origin") or "legacy",
                                "static_hits_since": int(item.get("static_hits_since", 0) or 0),
                            })
                    if records:
                        self._learned[str(group)] = records
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load learned overlay for %s: %s", self.provider, exc)

    def _is_expired(self, rec: dict) -> bool:
        learned_at = rec.get("learned_at")
        if not learned_at:
            # Legacy stamp: age unknown, keep it until static-success expiry shows
            # the maintained selector again works for this group.
            return False
        try:
            when = datetime.fromisoformat(str(learned_at))
        except ValueError:
            return False
        return (datetime.now() - when) > timedelta(
            days=config.LEARNED_SELECTOR_TTL_DAYS)

    def _prune_expired(self, name: str) -> list:
        """Drop expired learned records for a group; returns the removed records."""
        group = self._learned.get(name, [])
        if not group:
            return []
        removed = [r for r in group if self._is_expired(r)]
        if not removed:
            return []
        kept = [r for r in group if not self._is_expired(r)]
        if kept:
            self._learned[name] = kept
        else:
            self._learned.pop(name, None)
        self._save_learned()
        log.info("Expired %d learned selector(s) for '%s' (TTL %d days)",
                 len(removed), name, config.LEARNED_SELECTOR_TTL_DAYS)
        return removed

    def candidates(self, name: str) -> List[str]:
        """Ordered selector candidates for a group: fresh learned first, then static."""
        self._prune_expired(name)
        learned = [r["selector"] for r in self._learned.get(name, [])]
        static = self._defs.get(name, [])
        return list(dict.fromkeys([*learned, *static]))

    def learn(self, name: str, selector: str, origin: str = "recovery") -> None:
        """Persist a corrected selector for a group, stamped (R3-W2).

        ``origin`` records why/when it was learned (recovery agent, replay import,
        or a manual health-check fix) for the stale-entry audit trail.
        """
        static = self._defs.get(name, [])
        if not self._learned_file or selector in static:
            return
        if any(r.get("selector") == selector for r in self._learned.get(name, [])):
            return
        self._learned.setdefault(name, []).append({
            "selector": selector,
            "learned_at": datetime.now().isoformat(timespec="seconds"),
            "origin": origin,
            "static_hits_since": 0,
        })
        self._save_learned()
        log.info("Learned selector for '%s': %s (origin=%s)", name, selector, origin)

    def note_static_success(self, name: str) -> None:
        """Count a group resolution that succeeded via a *static* selector.

        After ``LEARNED_STATIC_HITS_TO_EXPIRE`` consecutive such successes the
        learned overlay for that group is redundant and is dropped (R3-F2).
        """
        if name not in self._learned or not self._learned[name]:
            return
        threshold = config.LEARNED_STATIC_HITS_TO_EXPIRE
        kept: List[dict] = []
        changed = False
        for rec in self._learned[name]:
            rec["static_hits_since"] = int(rec.get("static_hits_since", 0) or 0) + 1
            if rec["static_hits_since"] >= threshold:
                changed = True
                log.info("Learned selector '%s' for '%s' dropped after %d "
                         "consecutive static-success resolutions",
                         rec["selector"], name, threshold)
            else:
                kept.append(rec)
        if not changed:
            return
        if kept:
            self._learned[name] = kept
        else:
            self._learned.pop(name, None)
        self._save_learned()

    def _save_learned(self) -> None:
        try:
            self._learned_file.parent.mkdir(parents=True, exist_ok=True)
            self._learned_file.write_text(
                json.dumps(self._learned, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not persist learned overlay: %s", exc)

    # -- resolution ----------------------------------------------------------
    @staticmethod
    def _is_role_entry(selector: str) -> bool:
        return selector.startswith("get_by_role:")

    def _locate(self, page, selector: str):
        """Return the first matching locator for a candidate selector.

        Learned ``get_by_role:role:name`` entries are interpreted via Playwright's
        role locator rather than being passed to ``page.locator`` as CSS (they are
        not valid CSS selectors). Everything else is treated as a CSS selector.
        """
        if self._is_role_entry(selector):
            _, role, name = selector.split(":", 2)
            return page.get_by_role(role, name=name, exact=False).first
        return page.locator(selector).first

    def _on_resolve_success(self, name: str, selector: str) -> None:
        if selector in self._defs.get(name, []):
            self.note_static_success(name)

    def _try_candidate(self, page, name: str, selector: str, timeout: int,
                       hidden: bool = False, multi: bool = False):
        """Return a matching locator for one candidate, else None.

        Retries the *same* candidate once on a transient exception before the
        caller moves to the next selector (R3-W3), so a flaky scroll/timeout on
        the right element can't cause a fallback match against the wrong one.
        """
        for _ in range(2):
            try:
                loc = self._locate(page, selector)
                if multi:
                    if loc.count() > 0:
                        self._on_resolve_success(name, selector)
                        return loc
                    # counted zero: genuinely not on this page; try next candidate
                    return None
                if hidden:
                    loc.wait_for(state="attached", timeout=timeout)
                else:
                    loc.scroll_into_view_if_needed(timeout=timeout)
                self._on_resolve_success(name, selector)
                return loc
            except Exception:  # noqa: BLE001
                continue
        return None

    def _try_role(self, page, name: str, selector: str, role: str,
                  timeout: int):
        for _ in range(2):
            try:
                loc = page.get_by_role(role, name=selector, exact=False).first
                loc.scroll_into_view_if_needed(timeout=timeout)
                self._on_resolve_success(name, selector)
                return loc
            except Exception:  # noqa: BLE001
                continue
        return None

    def group(self, name: str) -> List[str]:
        return self.candidates(name)

    def first(self, name: str) -> Optional[str]:
        group = self.candidates(name)
        return group[0] if group else None

    def resolve(self, page, name: str, timeout: int = 5000):
        """Return the first matching locator for ``name`` or raise LookupError."""
        for selector in self.candidates(name):
            loc = self._try_candidate(page, name, selector, timeout)
            if loc is not None:
                return loc
        raise LookupError(f"No matching element for locator group '{name}'")

    def resolve_hidden(self, page, name: str, timeout: int = 5000):
        """Like ``resolve`` but for elements that are present yet hidden in the DOM
        (e.g. ``display:none`` file inputs). Such elements cannot be
        scrolled-into-view, so we skip that step."""
        for selector in self.candidates(name):
            loc = self._try_candidate(page, name, selector, timeout, hidden=True)
            if loc is not None:
                return loc
        raise LookupError(f"No matching (hidden) element for locator group '{name}'")

    def resolve_multi(self, page, name: str, timeout: int = 5000):
        """Return the first non-empty locator collection for ``name``."""
        for selector in self.candidates(name):
            loc = self._try_candidate(page, name, selector, timeout, multi=True)
            if loc is not None:
                return loc
        raise LookupError(f"No matching elements for locator group '{name}'")

    def try_resolve(self, page, name: str, timeout: int = 3000):
        """Best-effort resolve; returns None instead of raising."""
        try:
            return self.resolve(page, name, timeout=timeout)
        except LookupError:
            return None

    def role_first(self, page, name: str, role: str, timeout: int = 5000):
        """Resolve by semantic role + name; great for buttons/links."""
        for selector in self.candidates(name):
            loc = self._try_role(page, name, selector, role, timeout)
            if loc is not None:
                return loc
        raise LookupError(f"No role '{role}' element for locator group '{name}'")
