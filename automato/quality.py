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


def gate_assets(outputs: Dict[str, Any],
                requested: Optional[int] = None) -> Optional[QualityGate]:
    """At least ``MIN_IMAGE_FRACTION`` of requested assets must be captured.

    A total (0 images) capture is a hard-fail; a partial shortfall is a soft-fail.

    R8-B1: a ``degraded_reason`` output key (set when the assets stage used the
    keyless fallback provider) is an immediate soft-fail: fallback images may
    carry watermarks or be prompt-independent, so such a run must not go live
    unreviewed.
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
    elif stage_id == "assets":
        r = gate_assets(outputs, requested_images)
        if r:
            gates.append(r)
    elif stage_id == "assemble":
        r = gate_video(outputs, expected_video_s)
        if r:
            gates.append(r)
    return gates
