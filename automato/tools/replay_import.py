"""Import a @puppeteer/replay JSON (Chrome/Edge DevTools Recorder export).

The recorder's JSON ``steps`` array gives each interaction a list of candidate
``selectors`` (CSS and ARIA variants). This helper converts those into
``ProviderLocations``-style locator buckets that our resilience layer can consume,
so a one-time human recording can *seed* the semantic locators for an adapter or the
learned overlay — cutting hand-authoring time.

The output is a JSON object mapping a bucket name -> ordered selector list, e.g.:
    { "click_0": ["#create-button", "aria/Create"], ... , "url": "..." }

This is a seed-authoring aid, not a runtime dependency — the semantic locators in
each adapter remain the production source of truth, and any corrected selectors the
recovery agent finds later get merged into the same overlay.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


def _bucket_name(kind: str, index: int) -> str:
    prefix = {
        "click": "click",
        "change": "change",
        "type": "fill",
        "setFileInputFiles": "upload",
        "keyDown": "key",
    }.get(kind, kind.lower())
    return f"{prefix}_{index}"


def _selectors_for(step: dict) -> List[str]:
    """Extract ordered candidate selectors from a replay step."""
    out: List[str] = []
    sels = step.get("selectors")
    if isinstance(sels, list):
        for sel in sels:
            if isinstance(sel, list):
                for s in sel:
                    if isinstance(s, str) and s not in out:
                        out.append(s)
            elif isinstance(sel, str) and sel not in out:
                out.append(sel)
    # fall back to single selector field
    single = step.get("selector")
    if isinstance(single, str) and single not in out:
        out.append(single)
    return out


def import_replay(path: str, provider: Optional[str] = None,
                  out: Optional[str] = None) -> str:
    """Parse a recorded replay JSON and produce locator buckets.

    Returns a human-readable summary. If ``out`` is given, writes the buckets JSON
    there (or to the provider's learned overlay when ``provider`` is set and no out).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Replay file not found: {p}")

    data = json.loads(p.read_text(encoding="utf-8"))
    steps = data.get("steps", []) if isinstance(data, dict) else []
    if not steps:
        raise ValueError("No 'steps' found in replay JSON")

    buckets: Dict[str, List[str]] = {}
    start_url = ""
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_type = step.get("type", "")
        if step_type == "navigate":
            url = step.get("url")
            if url and not start_url:
                start_url = url
            continue
        if step_type in ("scroll", "waitForElement", "close", "doubleClick", "hover"):
            continue  # not needed for locator seeding / non-interactive
        sels = _selectors_for(step)
        if not sels:
            continue
        idx = sum(1 for k in buckets if k.split("_")[0] == _bucket_name(step_type, 0).split("_")[0])
        name = _bucket_name(step_type, idx)
        buckets[name] = sels
        # also record a value hint for fill/change so adapters know what to type
        val = step.get("value")
        if isinstance(val, str) and len(val) <= 200:
            buckets[f"{name}__value"] = [val]

    if start_url:
        buckets["url"] = [start_url]

    # write output
    target: Optional[Path] = None
    if out:
        target = Path(out)
    elif provider:
        from .. import config
        target = config.PROFILES_DIR / provider / "learned.json"
    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(buckets, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        wrote = str(target)
    else:
        wrote = "(stdout only)"

    lines = [f"Imported {len(steps)} steps -> {len(buckets)} locator buckets", f"Wrote: {wrote}"]
    for name, sels in buckets.items():
        lines.append(f"  {name}: {', '.join(sels)}")
    return "\n".join(lines)
