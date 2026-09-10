"""Turn narration word-boundary timings into caption intervals and caption files.

R8-B2 replaces the old "divide narration by caption count" guess with a real
sync: edge-tts streams WordBoundary events (``word_timings.json``) and this
module walks narration words in order, expanding each caption greedily onto the
narration words it covers most plausibly, so caption changes happen AT words, not
at fake 50% boundaries.

Fallback (honest, documented): when no real timing exists (a non-edge provider
served the run, or timing failed) we fall back to estimating each caption as a
slice of narration duration proportional to its character share -- the same
quality as before, but now explicit as "estimated" and holding the same
manifest binding, so the assembly stage never hard-fails.

Also renders the same timed captions as WebVTT and SRT files (R8-B6).
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

_WORD_SPLIT = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)


def _words(text: str) -> List[str]:
    return _WORD_SPLIT.findall(text or "")


def _timed_words(data: Optional[dict]) -> List[dict]:
    if not data:
        return []
    words = data.get("words") or []
    return [w for w in words if isinstance(w, dict) and w.get("word")]


def load_timings(path: "str | Path") -> dict:
    """Read a word_timings.json sidecar; an absent or unreadable file yields the
    empty/estimated shape rather than a crash."""
    p = Path(path)
    if not p.is_file():
        return {"provider": None, "words": [], "estimated": True}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"provider": None, "words": [], "estimated": True}


def plan_captions(captions: List[str], narration: str, timings: dict,
                  default_total: "float | None" = None) -> List[dict]:
    """Match each caption to a narration word interval.

    Returns ``[{index, text, start_s, end_s, estimated}]`` covering the whole
    narration, where ``estimated`` is True only when real timing was unavailable
    (proportional estimation). Both paths are deterministic and unit-free
    (seconds) so the assembly stage just consumes intervals. ``default_total``
    (e.g. the probed audio duration) anchors the estimated path when the sidecar
    has no duration of its own.
    """
    if not captions:
        return []
    timed = _timed_words(timings)
    if not timed:
        return _estimate(captions, narration, timings, default_total)
    return _greedy_match(captions, timed)


def _greedy_match(captions: List[str], timed: List[dict]) -> List[dict]:
    """Greedy forward match: narration words are consumed in order; each caption
    takes as many words as its own text shares, moving the caption boundary to a
    real narrated word. Cost is O(W*C) for the tiny sizes involved."""
    cap_tokens = [_words(c) for c in captions]
    pos = 0
    n_timed = len(timed)
    plan: List[dict] = []
    for cap_index, tokens in enumerate(cap_tokens):
        needed = max(len(tokens), 1)
        end_i = pos
        consumed = 0
        while consumed < needed and end_i < n_timed:
            consumed += 1
            end_i += 1
        if end_i >= n_timed:
            end_i = n_timed
        start_s = timed[pos]["start_s"] if pos < n_timed else 0.0
        end_s = timed[end_i - 1]["end_s"] if end_i - 1 >= 0 else start_s
        plan.append({
            "index": cap_index,
            "text": captions[cap_index],
            "start_s": round(float(start_s), 3),
            "end_s": round(float(end_s), 3),
            "estimated": False,
            "word_range": [pos, end_i],
        })
        pos = end_i
    return plan


def _estimate(captions: List[str], narration: str, timings: dict,
              default_total: "float | None" = None) -> List[dict]:
    """No real timing: divide the narration's total span proportionally to each
    caption's character share. Uses the sidecar's total_s when present, else the
    caller-supplied audio duration, else the pacing constant below."""
    total = float(timings.get("total_s") or 0.0) or float(default_total or 0.0) \
        or _estimate_total(narration)
    chars = sum(len(c) for c in captions) or 1
    plan: List[dict] = []
    cursor = 0.0
    for i, caption in enumerate(captions):
        span = (len(caption) / chars) * total
        end_s = cursor + span
        plan.append({
            "index": i,
            "text": caption,
            "start_s": round(cursor, 3),
            "end_s": round(end_s, 3),
            "estimated": True,
            "word_range": None,
        })
        cursor = end_s
    return plan


def _estimate_total(narration: str) -> float:
    # Rough narration duration when the sidecar lacks one (words/s baseline).
    return (len(_words(narration)) * 0.5) + 1.0


# ---------------------------------------------------------------------------
# Caption file writers (R8-B6): the SAME timed plan as the on-video captions,
# so viewers can follow along even with captions off.
# ---------------------------------------------------------------------------

def _ts(seconds: float) -> str:
    t = timedelta(seconds=max(0.0, seconds))
    total_ms = int(t.total_seconds() * 1000)
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def write_srt(plan: List[dict], path: "str | Path") -> str:
    blocks = []
    for i, seg in enumerate(plan, start=1):
        blocks.append(
            f"{i}\n{_ts(seg['start_s'])} --> {_ts(seg['end_s'])}\n{seg['text']}"
        )
    Path(path).write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return str(path)


def write_vtt(plan: List[dict], path: "str | Path") -> str:
    blocks = ["WEBVTT"]
    for seg in plan:
        blocks.append(
            f"{_ts(seg['start_s'])} --> {_ts(seg['end_s'])}\n{seg['text']}"
        )
    Path(path).write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return str(path)
