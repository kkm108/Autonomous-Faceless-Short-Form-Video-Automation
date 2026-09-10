"""Build the YouTube description, tags and chapters for a run's publish.

R8-B8: publishing uses this single source of truth written by the assembly stage
(``metadata.json``), so the description/tags/chapters always match the actual
video and captions in the run directory -- not a guess recomputed at publish
time. The publish adapter reads this file (falling back to today's script.json
shape for older runs) and uploads tags best-effort.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List


def _as_slug_word(word: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]", "", word).lower()
    return clean if len(clean) >= 4 else ""


def _derive_tags(title: str, captions: List[str], channel: str = "") -> List[str]:
    base = {"shorts", "faceless", "youtube shorts", "automation"}
    if channel:
        base.add(channel)
    for text in [title, *captions]:
        for w in text.split():
            sl = _as_slug_word(w)
            if sl:
                base.add(sl)
    ordering = ["shorts", *sorted(w for w in base if w != "shorts")]
    return ordering


def _chapters(captions_plan: List[dict]) -> List[dict]:
    """Chapter markers from the timed caption plan (offset >= 5s per YT rule)."""
    chapter_started = []
    for seg in captions_plan:
        ms = int(seg.get("start_s", 0.0) * 1000)
        if ms >= 5000 and (not chapter_started or ms - chapter_started[-1]["start_ms"] >= 5000):
            chapter_started.append({"start_ms": ms, "title": seg.get("text", "")[:48]})
    return chapter_started


def build_metadata(title: str,
                   captions_plan: List[dict],
                   topic: str = "",
                   channel: str = "") -> dict:
    """The full publish-metadata record persisted to metadata.json.

    Returned fields: title, description, tags, chapters, channel, topic. The
    description keeps the video honest: title line, a plain description of the
    topic, then chapters as standard YT timestamps when at least one marker
    passes the 5-second rule.
    """
    captions = [seg.get("text", "") for seg in captions_plan]
    description = [title, "", f"{topic.strip()} - a short, faceless explainer."]
    chapters = _chapters(captions_plan)
    if chapters:
        description.append("")
        description.append("Chapters:")
        description.extend(
            f"{int(c['start_ms'] / 1000 // 60):02d}:{int(c['start_ms'] / 1000 % 60):02d} {c['title']}"
            for c in chapters
        )
    if channel:
        description.append("")
        description.append(f"Watch more on {channel}.")
    return {
        "title": title,
        "description": "\n".join(description),
        "tags": _derive_tags(title, captions, channel),
        "chapters": chapters,
        "topic": topic,
        "channel": channel,
    }


def read_metadata(path: "str | Path") -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_metadata(meta: dict, path: "str | Path") -> str:
    Path(path).write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return str(path)
