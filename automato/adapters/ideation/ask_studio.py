"""Ask Studio topic ideation (R7).

Queries YouTube Studio's built-in Ask Studio assistant for a topic grounded in
the TARGET channel's own performance data, and hands the result back to the
existing scripting pipeline as the run's seed topic. Ask Studio refuses general
content requests, so the question is deliberately framed around the channel's
own metrics.

Flow (probed against the live Studio UI):
  1. Route the signed-in youtube session onto the target channel's Analytics
     page (Ask Studio lives there; it reflects the *active* channel).
  2. Open the assistant drawer (Ask Studio trigger, or an entrypoint chip).
  3. Type the channel-scoped question into the ``div[contenteditable]``
     composer and press Enter (Enter submits — verified).
  4. Capture the answer from the page body text that appends after the pre-send
     anchor, waiting for it to settle (streams in tens of seconds).
  5. Parse a concrete topic out of the answer and seed it via ``ctx.seed`` so
     the scripting stage (bound to ``seed.topic``) picks it up with no
     workflow-manifest changes.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from ... import config
from ...channels import channel_id_for, ensure_channel
from ..base import ExecutorError

log = logging.getLogger(__name__)

_TOPIC_MARKERS = (
    "Recommended Concrete Topic",
    "Recommended topic",
    "Recommended Topic",
    "Suggested topic",
    "Suggested Topic",
    "Here is your topic",
    "Here's your topic",
    "Your topic",
    "Topic:",
    "topic:",
)

_REFUSAL_PHRASES = (
    "i can't",
    "i am not able",
    "i'm not able",
    "cannot help",
    "can't help",
    "don't have access",
    "do not have access",
    "unable to provide",
)

# Interim chat states (Ask Studio streams these as it works) and the trainer
# disclaimer that footers every assistant card — none of them is an answer.
_STREAM_STATUS_PHRASES = (
    "gathering the comments",
    "gathering comments",
    "thinking",
    "analyzing",
    "preparing",
    "searching your channel",
    "reviewing your analytics",
)
_DISCLAIMER_PHRASES = (
    "ai can make mistakes",
    "you are responsible for the content you publish",
    "learn more",
)


def parse_topic(reply: str) -> str:
    """Extract a concrete topic from an Ask Studio answer.

    Ask Studio's answers are narrative; the most defensible extraction is the
    title-like phrase after a "Recommended <x> Topic" marker. Among the
    following lines, the *longest* non-sentence phrase wins — the concrete
    title ("Fast Paced Stop The Timer Visual Reflex Number Challenge") beats
    the descriptor line ("Recent high engagement on interactive number
    puzzles") and commentary paragraphs.
    """
    text = (reply or "").strip()
    if not text:
        return ""
    for marker in _TOPIC_MARKERS:
        idx = text.find(marker)
        if idx < 0:
            continue
        rest = text[idx + len(marker):].lstrip(":. \n\t\u2013\u2014")
        candidates = [
            c for c in _candidate_lines(rest) if not _is_boilerplate(c)
        ]
        if candidates:
            titles = [c for c in candidates if _is_title_case(c)]
            return _clip90(max(titles or candidates, key=len))
        break
    # No marker (Ask Studio varies its formatting): fall back to a short
    # candidate line, preferring a Title-Case phrase — those are the concrete
    # topic titles the card lists (observed live: "AI Math Riddle That 95
    # Percent of Viewers Solve Completely Wrong" beats the nested sentences).
    candidates = [c for c in _candidate_lines(text) if not _is_boilerplate(c)]
    if candidates:
        titles = [c for c in candidates if _is_title_case(c)]
        return _clip90(max(titles or candidates, key=len))
    return ""


def _clip90(phrase: str) -> str:
    """Clip to <=90 chars without splitting a word."""
    phrase = phrase.strip()
    if len(phrase) <= 90:
        return phrase
    cut = phrase.rfind(" ", 0, 90)
    return phrase[: (cut if cut > 40 else 90)].strip()


def _is_title_case(line: str) -> bool:
    """A candidate topic looks like a title when most of its words start
    uppercase (headline case rather than a narrative sentence). Numeric tokens
    (dates, percentages) don't count against it."""
    tokens = [w for w in re.split(r"\s+", re.sub(r"[^\w]", " ", line)) if w]
    words = [w for w in tokens if any(ch.isalpha() for ch in w)]
    if len(words) < 3:
        return False
    upper = sum(1 for w in words if w and w[0].isupper())
    return upper / len(words) >= 0.7


def _is_boilerplate(line: str) -> bool:
    low = line.lower()
    return any(p in low for p in _STREAM_STATUS_PHRASES) or any(
        p in low for p in _DISCLAIMER_PHRASES)


def _strip_question_echo(text: str, question: str) -> str:
    """Remove the echoed question from the front of a captured chat region.

    After sending, the chat renders the user's own question as a bubble INSIDE
    the captured region (the pre-send anchor sits above it). ``inner_text`` can
    even wrap a bubble mid-word (observed: ``behavior,`` read back as
    ``havior,``). Lines are dropped from the front while most of their word
    stems appear in the question.
    """
    if not (text and question):
        return text or ""
    q_words = [
        w for w in re.split(r"\s+", re.sub(r"[^\w\s']", " ", question.lower())) if w
    ]
    if not q_words:
        return text
    lines = text.split("\n")
    while lines:
        line = lines[0]
        words = [w for w in re.split(r"\s+", re.sub(r"[^\w\s']", " ", line.lower())) if w]
        if not words:
            lines.pop(0)
            continue
        # A line is an echo fragment if most of its word-stems overlap question
        # stems — including a wrap-split word ("havior," inside "behavior,").
        overlap = sum(
            1 for w in words
            if any(c in w or w in c for c in q_words)
        ) / len(words)
        if overlap >= 0.6:
            lines.pop(0)
            continue
        break
    return "\n".join(lines)


def _candidate_lines(text: str) -> list:
    """Lines that look like a concrete topic phrase (4..90 chars, not a
    commentary sentence/section word, trailing punctuation tolerated)."""
    lines: list = []
    for raw in text.splitlines():
        clean = raw.strip().rstrip(".").lstrip("*\u2022\uf0b7- \u2011 ").strip()
        if not clean or clean in ("",):
            continue
        if 4 <= len(clean) <= 90 and not clean.endswith((".", ":")):
            lines.append(clean)
    return lines


def _is_refusal(reply: str) -> bool:
    low = reply.lower()
    return any(phrase in low for phrase in _REFUSAL_PHRASES)


def _open_drawer(page, settings) -> None:
    """Open the Ask Studio assistant drawer (no question auto-sent)."""
    trig_sel = "ytcp-creator-chat-trigger, #ytcpCreatorChatTriggerHost"
    deadline = time.time() + config.ASK_STUDIO_COMPOSER_WAIT_S
    last_err: Optional[Exception] = None
    while time.time() < deadline:
        try:
            trig = page.locator(trig_sel)
            if trig.count() > 0:
                trig.first.click(timeout=6000)
                last_err = None
                break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        try:
            chip = page.get_by_text("Summarize my latest video performance",
                                    exact=True).first
            if chip.count() > 0:
                chip.click(timeout=6000)
                last_err = None
                break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(2)
    if last_err is not None:
        raise ExecutorError(
            f"Could not open the Ask Studio assistant drawer: {last_err}",
            retryable=True,
        )


def _locate_composer(page, settings) -> Optional[Any]:
    deadline = time.time() + config.ASK_STUDIO_COMPOSER_WAIT_S
    while time.time() < deadline:
        for sel in ("div[contenteditable='true']", "textarea"):
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    return loc.first
            except Exception:  # noqa: BLE001
                continue
        time.sleep(2)
    return None


def _is_final_answer(text: str) -> bool:
    """A captured region is a REAL answer (not just the question echo, a
    stream-status card, or the disclaimer footer). A topic marker always
    counts; otherwise require a minimal body of novel text. Lets the capture
    keep waiting while Ask Studio is still "gathering comments"."""
    if not (text or "").strip():
        return False
    if any(marker.lower() in text.lower() for marker in _TOPIC_MARKERS):
        return True
    t = text
    for line in t.splitlines():
        if _is_boilerplate(line):
            continue
        stripped = (line.strip() or "").rstrip(".")
        if len(stripped) >= 40:
            return True
    return False


def _capture_reply(page, anchor: str, question: str, settings) -> str:
    started = time.time()
    last = ""
    stable = 0
    deadline_until = time.time() + config.ASK_STUDIO_REPLY_WAIT_S
    while time.time() < deadline_until:
        time.sleep(4)
        try:
            body = page.locator("body").inner_text(timeout=5000)
        except Exception:  # noqa: BLE001
            continue
        if len(body) <= len(anchor):
            continue
        region = _strip_question_echo(body[len(anchor):], question)
        # Interim cards ("Gathering the comments...", the disclaimer) are the
        # same text poll after poll even while the real answer is still being
        # produced, so require a genuine answer before treating it as settled.
        if not _is_final_answer(region):
            last = region
            continue
        if region != last:
            last = region
            stable = 0
        else:
            stable += 1
        # Settled once the answer stopped growing (>=3 stable polls).
        if stable >= 3 and len(region) > 110:
            break
    log.info("Ask Studio reply captured in %.0fs (%d chars)",
             time.time() - started, len(last))
    return last


def _ask(page, question: str, settings) -> str:
    composer = _locate_composer(page, settings)
    if composer is None:
        raise ExecutorError(
            "Ask Studio drawer opened but no composer was found; cannot ask the "
            "ideation question.",
            retryable=True,
        )
    # Anchor to body text BEFORE typing so the reply region is everything after
    # the pre-send page state.
    try:
        anchor = page.locator("body").inner_text(timeout=5000) or ""
    except Exception:  # noqa: BLE001
        anchor = ""
    try:
        composer.click(timeout=6000)
        try:
            composer.fill(question, timeout=6000)
        except Exception:  # noqa: BLE001
            composer.press_sequentially(question, delay=3)
        composer.press("Enter")
    except Exception as exc:  # noqa: BLE001
        raise ExecutorError(f"Could not submit the Ask Studio question: {exc}",
                            retryable=True) from exc
    return _capture_reply(page, anchor, question, settings)


def run(ctx, inputs, run_dir, session=None):
    """Ask the active channel's Ask Studio assistant for a topic; seed it.

    Channel + question come from the validated per-run settings (``ctx.settings``),
    resolved by the orchestrator. ``inputs`` is intentionally unused: the engine's
    binding resolver would rewrite bare string inputs into run_dir paths.
    """
    channel_id = channel_id_for(ctx.settings.channel_name) if ctx.settings.channel_name else None
    question = ctx.settings.ask_studio_question or config.ASK_STUDIO_QUESTION
    if not channel_id:
        raise ExecutorError(
            "Ask Studio ideation needs a target channel (resolved via the "
            "channel allowlist); none was available.",
            retryable=False,
        )
    if session is None:
        raise ExecutorError(
            "Ask Studio ideation requires the signed-in youtube session.",
            retryable=False,
        )

    page = session.first_page()
    page.goto(f"https://studio.youtube.com/channel/{channel_id}/analytics",
              wait_until="domcontentloaded", timeout=45000)
    time.sleep(config.YOUTUBE_UI_SETTLE_S)
    ensure_channel(page, channel_id)
    _open_drawer(page, ctx.settings)
    raw = _ask(page, question, ctx.settings)

    # ''raw'' has already had the echoed question and stream-status/disclaimer
    # cards stripped; both refuse detection and topic parsing work on the
    # assistant's own words.
    if _is_refusal(raw) and not any(m.lower() in raw.lower()
                                    for m in _TOPIC_MARKERS):
        raise ExecutorError(
            "Ask Studio did not provide a topic (it answered with a refusal: "
            f"{raw.strip()[:200]}). Ask Studio refuses general content "
            "requests; the ideation question must stay scoped to the channel's "
            "own performance metrics.",
            retryable=False,
        )
    topic = parse_topic(raw) or ""
    if not topic:
        raise ExecutorError(
            "Ask Studio answered but no concrete topic could be parsed from: "
            f"{raw.strip()[:300]}",
            retryable=True,
        )

    # Feed the existing scripting pipeline (bound to seed.topic) with no manifest
    # changes: ctx.seed IS the run's mutable seed dict.
    ctx.seed["topic"] = topic
    ctx.seed["topic_source"] = "ask_studio"
    ctx.seed["topic_raw"] = raw.strip()
    ctx.seed["topic_channel_id"] = channel_id

    out_path = run_dir / "ideation.json"
    out_path.write_text(json.dumps({
        "provider": "ask_studio",
        "channel_id": channel_id,
        "question": question,
        "topic": topic,
        "raw": raw.strip(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Ask Studio ideation (channel %s) -> topic: %r", channel_id, topic)
    return {
        "topic": topic,
        "provider": "ask_studio",
        "channel_id": channel_id,
        "ideation": str(out_path),
        "raw": raw.strip(),
    }
