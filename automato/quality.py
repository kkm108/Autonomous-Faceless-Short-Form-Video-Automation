"""Output quality gates (R2-W4/R2-F4).

Each gate is a pure function of a completed stage's outputs and returns
``(ok: bool, message: str)``. The orchestrator runs the applicable gate after the
stage completes and, on failure, either hard-fails (for mandatory thresholds) or
soft-fails (which forces the publish stage to downgrade to ``unlisted`` — a last
safety net even when a run explicitly opted into ``--visibility public``).

Rationale: a stage returning *a file* is not the same as returning *a publishable
artifact*. A truncated script or a silently-quiet video should never go fully live
unreviewed. The engine's default visibility is already ``unlisted``; this turns
that safety instinct into an enforced floor.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import config

log = logging.getLogger(__name__)

# Minimum narration length and required terminal punctuation (R2-W4).
MIN_NARRATION_WORDS = 40
_TERMINAL_PUNCT = (".", "!", "?")
# Hard threshold: fewer than this fraction of requested images is a hard-fail.
MIN_IMAGE_FRACTION = 0.8
# Allowed audio-slack around the expected duration (seconds), both directions.
DURATION_TOLERANCE_S = 5.0
# Post-assembly audio must have at least this much peak volume to count as audible.
MIN_AUDIO_PEAK = 0.02


class QualityGate:
    """Result of a single quality check."""

    __slots__ = ("ok", "message", "hard")

    def __init__(self, ok: bool, message: str, hard: bool = False):
        self.ok = ok
        self.message = message
        self.hard = hard


def _is_terminal(text: str) -> bool:
    t = text.rstrip()
    return t.endswith(_TERMINAL_PUNCT)


def gate_script(outputs: Dict[str, Any]) -> Optional[QualityGate]:
    """Narration must be a reasonably complete script (soft-fail if not)."""
    path = Path(outputs.get("script", ""))
    if not path.is_file():
        return QualityGate(False, "script.json missing", hard=True)
    try:
        script = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return QualityGate(False, f"script.json unreadable: {exc}", hard=True)
    narration = str(script.get("spoken_script", "") or "")
    words = narration.split()
    if len(words) < MIN_NARRATION_WORDS:
        return QualityGate(
            False,
            f"narration too short ({len(words)} < {MIN_NARRATION_WORDS} words)",
        )
    if not _is_terminal(narration):
        return QualityGate(False, "narration does not end in terminal punctuation")
    if not script.get("captions"):
        return QualityGate(False, "no captions in script", hard=True)
    return None


def _image_file_hashes(files) -> list:
    """SHA-256 of each readable file's bytes (skipping unreadable/missing paths,
    which the earlier count checks already hold accountable)."""
    out = []
    for p in files:
        try:
            out.append(hashlib.sha256(Path(p).read_bytes()).hexdigest())
        except OSError:
            continue
    return out


def gate_assets(outputs: Dict[str, Any],
                requested: Optional[int] = None) -> Optional[QualityGate]:
    """At least ``MIN_IMAGE_FRACTION`` of requested assets must be captured.

    A total (0 images) capture is a hard-fail; a partial shortfall is a soft-fail.

    R8-B1: a ``degraded_reason`` output key (set when the assets stage used the
    keyless fallback provider) is an immediate soft-fail: fallback images may
    carry watermarks or be prompt-independent, so such a run must not go live
    unreviewed.

    R10-P0: the captured slide images must be content-*distinct*. The gate used to
    check only the image *count*, so six reuses of one image passed silently —
    which is exactly the image/mismatch defect the last review found. Any pair of
    byte-identical images in one video's asset set is now a soft-fail: a human
    reviews before it can go public. (Perchance already de-duplicates within a
    batch, so a duplicate here means the generator reused/collapsed across prompts
    or the fallback served the same image twice.)
    """
    degraded = outputs.get("degraded_reason")
    if degraded:
        return QualityGate(False, f"assets fell back to another provider: {degraded}")
    files = outputs.get("image_files") or []
    if isinstance(files, str):
        assets = Path(files)
        files = sorted(p for p in assets.glob("bg_*.jpg") if p.is_file())
    total = len(files)
    if total == 0:
        return QualityGate(False, "no background images captured", hard=True)
    if requested and total < requested * MIN_IMAGE_FRACTION:
        return QualityGate(
            False,
            f"captured {total}/{requested} image(s) ({100*total//max(1, requested)}%), "
            f"below {100*MIN_IMAGE_FRACTION:.0f}% threshold",
        )
    hashes = _image_file_hashes(files)
    if len(hashes) > 1 and len(set(hashes)) < len(hashes):
        dups = len(hashes) - len(set(hashes))
        return QualityGate(
            False,
            f"{dups}/{total} captured images are byte-identical (a slide would "
            "reuse another slide's art); the asset set must be distinct",
        )
    return None


def _probe_streams(path: str) -> Tuple[bool, bool, float]:
    """Return (has_video, has_audio, peak_volume). Peak volume uses ffmpeg's
    volumedetect on the audio stream; -inf (silence) => 0.0 peak."""
    has_video = has_audio = False
    peak = 0.0
    try:
        vp = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=int(config.FFMPEG_TIMEOUT_S),
        )
        has_video = "video" in vp.stdout
        ap = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=int(config.FFMPEG_TIMEOUT_S),
        )
        has_audio = "audio" in ap.stdout
        if has_audio:
            vol = subprocess.run(
                ["ffmpeg", "-i", path, "-map", "0:a:0", "-af", "volumedetect",
                 "-f", "null", "-"],
                capture_output=True, text=True, timeout=int(config.FFMPEG_TIMEOUT_S),
            )
            for line in vol.stderr.splitlines():
                if "mean_volume" in line or "max_volume" in line:
                    m = re.search(r"([-+]?\d+(?:\.\d+)?) dB", line)
                    if m:
                        db = float(m.group(1))
                        peak = max(peak, 10 ** (db / 20))
    except Exception as exc:  # noqa: BLE001
        log.warning("Stream probe failed (%s); assuming silence/absent", exc)
    return has_video, has_audio, peak


def gate_video(outputs: Dict[str, Any], expected_s: Optional[float]) -> Optional[QualityGate]:
    """Final video must have both a video and a non-silent audio stream of roughly
    the expected duration (soft-fail on any deviation; missing file is hard-fail)."""
    path = Path(outputs.get("video", ""))
    if not path.is_file():
        return QualityGate(False, "final video missing", hard=True)
    has_video, has_audio, peak = _probe_streams(str(path))
    if not has_video:
        return QualityGate(False, "final video has no video stream", hard=True)
    if not has_audio:
        return QualityGate(False, "final video has no audio stream")
    if peak < MIN_AUDIO_PEAK:
        return QualityGate(False, f"final video audio appears silent (peak {peak:.4f})")
    if expected_s:
        dur = _duration_of(str(path))
        if abs(dur - expected_s) > DURATION_TOLERANCE_S:
            return QualityGate(
                False,
                f"final video duration {dur:.1f}s deviates from expected {expected_s:.1f}s",
            )
    return None


def _duration_of(path: str) -> float:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=int(config.FFMPEG_TIMEOUT_S),
        )
        return float(proc.stdout.strip())
    except Exception:  # noqa: BLE001
        return 0.0


def gate_degraded(outputs: Dict[str, Any]) -> Optional[QualityGate]:
    """A ``degraded_reason`` output key is a soft-fail for any stage.

    R11-W2: fallback providers for the voiceover chain (soundtools/pyttsx3) are
    lower fidelity than the zero-copy edge-tts primary, so a run whose narration
    was only produced by a weight roll that split it to a fallback — in health
    mode, or after an actual provider failure in either mode — must not go live
    unreviewed. Exactly like ``gate_assets``, it downgrades the publish to
    unlisted rather than blocking the run (soft-fail).
    """
    degraded = outputs.get("degraded_reason")
    if degraded:
        return QualityGate(False, f"stage reported a degraded fallback: {degraded}")
    return None


def gate_factcheck(outputs: Dict[str, Any]) -> Optional[QualityGate]:
    """R10-P2: a high-tier script whose review flagged a claim — or whose review
    pass got no reply at all — must not go live public: soft-fail (downgrade to
    unlisted) so a human reviews it first.

    Deliberately never a hard-block: the underlying model review is imperfect,
    and falling back to unlisted makes a genuinely dangerous script fail closed
    without aborting the run. An *absent* artifact is untouched (low-tier
    channels and disabled fact-check never write one, and entertainment content
    isn't slowed down by a check built for a different risk). But once a review
    file exists it proves a high-tier pass ran, and an empty reply from every
    provider is not evidence of safety — it is the dead-chat case, indistinguish-
    able from a skipped pass, so it downgrades exactly like a flagged claim.
    """
    path = Path(outputs.get("review", ""))
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    # Only high-tier channels ever produce a review artifact; a missing
    # risk_tier marker therefore defaults to "high" (fail-closed).
    tier = str(data.get("risk_tier") or "high").strip().lower()
    if tier != "high":
        return None
    if str(data.get("status") or "").strip() == "unreviewed":
        return QualityGate(
            False,
            "high-tier fact-check review produced no reply; the script ran "
            "unverified and needs a human before it can go public",
        )
    if not data.get("flagged"):
        return None
    flags = [str(f)[:120] for f in (data.get("flags") or [])]
    detail = "; ".join(flags[:3]) or "the review pass asked for verification"
    return QualityGate(False, f"script fact-check flagged: {detail}")


def run_gates(stage_id: str, outputs: Dict[str, Any],
              requested_images: Optional[int] = None,
              expected_video_s: Optional[float] = None) -> list:
    """Run the gate(s) applicable to a completed stage. Returns a list of
    non-passing QualityGate results (empty list = all good). Hard failures are
    included with ``hard=True`` so the caller can decide fail vs. downgrade."""
    gates: list = []
    if stage_id == "script":
        r = gate_script(outputs)
        if r:
            gates.append(r)
        r = gate_factcheck(outputs)
        if r:
            gates.append(r)
    elif stage_id == "assets":
        r = gate_assets(outputs, requested_images)
        if r:
            gates.append(r)
    elif stage_id == "voiceover":
        r = gate_degraded(outputs)
        if r:
            gates.append(r)
    elif stage_id == "assemble":
        r = gate_video(outputs, expected_video_s)
        if r:
            gates.append(r)
    return gates
