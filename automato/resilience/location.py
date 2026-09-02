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
"""
from __future__ import annotations

import json
import logging
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
        # {locator_group: [selector, ...]} discovered at runtime
        self._learned: Dict[str, List[str]] = {}
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
                if isinstance(data, dict):
                    self._learned = {
                        str(k): list(v) for k, v in data.items() if isinstance(v, list)
                    }
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load learned overlay for %s: %s", self.provider, exc)

    def candidates(self, name: str) -> List[str]:
        """Ordered selector candidates for a group: learned first, then static."""
        learned = self._learned.get(name, [])
        static = self._defs.get(name, [])
        return list(dict.fromkeys([*learned, *static]))

    def learn(self, name: str, selector: str) -> None:
        """Persist a corrected selector for a group (skips duplicate/static)."""
        static = self._defs.get(name, [])
        if not self._learned_file or selector in static:
            return
        group = self._learned.setdefault(name, [])
        if selector not in group:
            group.append(selector)
            self._save_learned()
            log.info("Learned selector for '%s': %s", name, selector)

    def _save_learned(self) -> None:
        try:
            self._learned_file.parent.mkdir(parents=True, exist_ok=True)
            self._learned_file.write_text(
                json.dumps(self._learned, ensure_ascii=False, indent=2), encoding="utf-8")
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

    def group(self, name: str) -> List[str]:
        return self.candidates(name)

    def first(self, name: str) -> Optional[str]:
        group = self.candidates(name)
        return group[0] if group else None

    def resolve(self, page, name: str, timeout: int = 5000):
        """Return the first matching locator for ``name`` or raise LookupError."""
        for selector in self.candidates(name):
            try:
                loc = self._locate(page, selector)
                loc.scroll_into_view_if_needed(timeout=timeout)
                return loc
            except Exception:  # noqa: BLE001
                continue
        raise LookupError(f"No matching element for locator group '{name}'")

    def resolve_hidden(self, page, name: str, timeout: int = 5000):
        """Like ``resolve`` but for elements that are present yet hidden in the DOM
        (e.g. ``display:none`` file inputs). Such elements cannot be
        scrolled-into-view, so we skip that step."""
        for selector in self.candidates(name):
            try:
                loc = self._locate(page, selector)
                loc.wait_for(state="attached", timeout=timeout)
                return loc
            except Exception:  # noqa: BLE001
                continue
        raise LookupError(f"No matching (hidden) element for locator group '{name}'")

    def resolve_multi(self, page, name: str, timeout: int = 5000):
        """Return the first non-empty locator collection for ``name``."""
        for selector in self.candidates(name):
            try:
                loc = self._locate(page, selector)
                if loc.count() > 0:
                    return loc
            except Exception:  # noqa: BLE001
                continue
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
            try:
                loc = page.get_by_role(role, name=selector, exact=False).first
                loc.scroll_into_view_if_needed(timeout=timeout)
                return loc
            except Exception:  # noqa: BLE001
                continue
        raise LookupError(f"No role '{role}' element for locator group '{name}'")
