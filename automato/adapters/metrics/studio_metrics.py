"""Per-video performance analytics from the signed-in Studio session (R11-W3).

The A/B-test layer judges "did the provider the weight roll picked actually
perform" by correlating each run's recorded provider choice with its post-publish
views and average view duration. Analytics are pulled ~7 days after publish from
the already-authenticated YouTube Studio session (the same browser profile the
publish stage used).

Only the analytics *highlight cards* are parsed (HTML text, not the canvas
charts), so the parsing is a pure function of the page's visible text and is
fully unit-tested without a browser. A brand-new watch page legitimately has no
analytics yet; ``fetch_metrics`` marks that ``no_data`` instead of erroring, so
the correlation pass never aborts on "too recent".
"""
from __future__ import annotations

import logging
import re
import time

log = logging.getLogger(__name__)

_VIEWS_INLINE_RE = re.compile(r"^([\d,]+(?:\.\d+)?[KMB]?)\s+views?$", re.IGNORECASE)
_DURATION_RE = re.compile(r"^(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{1,2})$")


def _to_number(s: str) -> "int | None":
    """Parse a Studio card value like ``1,234`` / ``1.2K`` / ``3.4M``.

    Returns ``None`` (not an exception) for text that is not a number, so the
    value-after-label scan can just skip unrelated lines.
    """
    token = (s or "").strip().replace(",", "").upper()
    if not token or token in ("-", "--", "N/A"):
        return None
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    if token[-1] in mult:
        try:
            return int(float(token[:-1]) * mult[token[-1]])
        except ValueError:
            return None
    try:
        return int(float(token))
    except ValueError:
        return None


def _parse_duration(s: str) -> "float | None":
    """Parse ``M:SS`` / ``H:MM:SS`` (avg-view-duration cards) into seconds."""
    token = (s or "").strip().replace(",", "")
    if not token:
        return None
    m = _DURATION_RE.match(token)
    if m:
        hours = int(m.group("h") or 0)
        return float(hours * 3600 + int(m.group("m")) * 60 + int(m.group("s")))
    num = _to_number(token)
    return float(num) if num is not None else None


def _value_after(lines, label_words, parse):
    """Scan ``lines`` for a card label, then the next parseable value line."""
    for i, line in enumerate(lines):
        lowered = line.lower()
        if all(word in lowered for word in label_words) and len(line) <= 60:
            for j in range(i + 1, min(i + 4, len(lines))):
                value = parse(lines[j])
                if value is not None:
                    return value
    return None


def parse_metrics(body_text: str) -> dict:
    """Extract the analytics highlight cards from a page's flattened text.

    Returns ``{"views": int|None, "avg_view_duration_s": float|None}``. A ``None``
    field means the card was not present or not parseable — the caller decides
    whether that is "no data yet" (fresh publish) or an extraction miss.
    """
    lines = [ln.strip() for ln in re.split(r"\n+", (body_text or "").strip())
             if ln.strip()]
    views = None
    for line in lines:
        m = _VIEWS_INLINE_RE.match(line)
        if m:
            views = _to_number(m.group(1))
            break
    if views is None:
        views = _value_after(lines, ("views",), _to_number)
    avgret = _value_after(lines, ("view", "duration"), _parse_duration)
    return {"views": views, "avg_view_duration_s": avgret}


def fetch_metrics(page, video_id: str, timeout_ms: int = 45000) -> dict:
    """Open a published video's Studio analytics page and read the highlight cards.

    The page is scoped to the signed-in profile's active channel; a video id that
    does not belong to that channel simply shows no data and returns ``no_data``.
    """
    url = f"https://studio.youtube.com/video/{video_id}/analytics"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        time.sleep(4)
        try:
            text = page.locator("body").inner_text(timeout=15000)
        except Exception:  # noqa: BLE001
            text = ""
    except Exception as exc:  # noqa: BLE001
        log.warning("Analytics fetch failed for %s: %s", video_id, exc)
        return {"video_id": video_id, "status": "error", "error": str(exc)}
    parsed = parse_metrics(text)
    parsed["video_id"] = video_id
    parsed["fetched_at"] = time.time()
    parsed["status"] = "ok" if parsed.get("views") is not None else "no_data"
    return parsed
