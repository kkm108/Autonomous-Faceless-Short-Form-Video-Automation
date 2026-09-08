"""Assemble the final 1080x1920 vertical video with local FFmpeg + Pillow.

This stage is pure local computation (not an "external task"), so it runs without
a browser. It:
  1. Renders one captioned slide per script caption (background image + text).
  2. Measures voiceover duration with ffprobe.
  3. Constructs an image-sequence video with a subtle Ken Burns zoom, sized 9:16,
     advancing each slide to match the narration, and muxes the audio to exactly
     fill the clip.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ... import config

log = logging.getLogger(__name__)

W, H = 1080, 1920
FPS = 30
FONT_SIZE = 92
CAPTION_STYLE = {
    "lines": 3,
}

# System fonts are preferred where present; the bundled open-license DejaVu Sans
# (automato/adapters/assembly/fonts/DejaVuSans.ttf) is the universal cross-platform
# fallback so caption rendering never silently degrades to PIL's low-quality bitmap
# font just because a runner isn't Windows (R2-W1).
_BUNDLED_FONT = Path(__file__).resolve().parent / "fonts" / "DejaVuSans.ttf"

FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("C:/Windows/Fonts/tahoma.ttf"),
    _BUNDLED_FONT,
]


def _run(cmd: list, timeout_s: int = None):
    """Run an ffmpeg/ffprobe subprocess with a timeout, logging stderr on error.

    R1-W7: a stuck encode previously hung the pipeline forever and swallowed the
    actual ffmpeg error (subprocess.run with ``capture_output=True`` raises with a
    useless exit-status message). Now every call is bounded by
    ``config.FFMPEG_TIMEOUT_S`` and, on failure, the real stderr is logged.
    """
    timeout_s = timeout_s or int(config.FFMPEG_TIMEOUT_S)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install ffmpeg "
            "(e.g. 'winget install ffmpeg' / 'apt install ffmpeg') and retry."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        log.error("Command exceeded %ss timeout: %s", timeout_s, " ".join(cmd))
        raise RuntimeError(
            f"ffmpeg timed out after {timeout_s}s: {' '.join(cmd)}"
        ) from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        log.error("Command failed (rc=%s): %s\n%s",
                  proc.returncode, " ".join(cmd), stderr or "(no stderr)")
        raise RuntimeError(f"ffmpeg command failed: {stderr or '(no stderr)'}")
    return proc


def _load_font(size: int):
    for cand in FONT_CANDIDATES:
        if cand.is_file():
            if cand == _BUNDLED_FONT:
                log.info("No system font found; using bundled DejaVu Sans fallback")
            return ImageFont.truetype(str(cand), size)
    log.warning("No TTF font available at all; using PIL default bitmap font "
                "(low quality captions)")
    return ImageFont.load_default()


def _draw_text_wrapped(draw, text, box_w, box_h, font, fill):
    """Center-and-wrap `text` into the given box, splitting on spaces.

    Over-long captions keep the *start* (so a published caption never begins
    mid-sentence) and drop overflowing trailing lines, signalling truncation with
    a trailing ellipsis.
    """
    words = text.split()
    if not words:
        return
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= box_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    # drop overflowing lines (keep FIRST lines; truncate the tail).
    truncated = len(lines) > CAPTION_STYLE["lines"]
    while len(lines) > CAPTION_STYLE["lines"]:
        lines.pop()
    # trim the final kept line so it fits with an ellipsis appended.
    ell = "\u2026"
    if truncated and lines:
        while lines and draw.textlength(lines[-1] + ell, font=font) > box_w:
            lines[-1] = lines[-1][:-1].rstrip()
            if not lines[-1]:
                lines.pop()
                break
    if truncated and lines and not lines[-1].endswith(ell):
        lines[-1] = lines[-1] + ell
    if not lines:
        return
    line_h = font.size * 1.15
    total_h = line_h * len(lines)
    y = (box_h - total_h) / 2
    for ln in lines:
        w_ln = draw.textlength(ln, font=font)
        x = (box_w - w_ln) / 2
        # soft shadow for readability
        draw.text((x + 4, y + 4), ln, font=font, fill=(0, 0, 0, 180))
        draw.text((x, y), ln, font=font, fill=fill)
        y += line_h


def _render_slide(bg_path: Path, caption: str, out_path: Path):
    bg = Image.open(bg_path).convert("RGB")
    bg = bg.resize((W, H), Image.LANCZOS)
    # subtle dark overlay for text contrast
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([0, 0, W, H], fill=(0, 0, 0, 90))
    composite = Image.alpha_composite(bg.convert("RGBA"), overlay)

    draw = ImageDraw.Draw(composite)
    font = _load_font(FONT_SIZE)
    box_w = W - 120
    box_h = 700
    _draw_text_wrapped(draw, caption, box_w, box_h, font, (255, 255, 255))
    composite.convert("RGB").save(out_path)
    log.debug("Rendered slide -> %s", out_path.name)


def _probe_duration(path: Path) -> float:
    proc = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
    )
    try:
        return float(proc.stdout.strip())
    except Exception:  # noqa: BLE001
        return 0.0


def run(ctx, inputs, run_dir, session=None):
    script = json.loads(Path(inputs["script"]).read_text(encoding="utf-8"))
    audio_path = Path(inputs["audio"])
    assets_dir = Path(inputs["images"])

    images = sorted(p for p in assets_dir.glob("bg_*.jpg") if p.is_file())
    if not images:
        raise RuntimeError("No background assets found for assembly")
    captions = script.get("captions", [])

    duration = _probe_duration(audio_path)
    if duration <= 0:
        raise RuntimeError("Could not determine audio duration")

    # Build slides: cycle images across captions; pad/limit captions to slides.
    slides_dir = run_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    slide_paths = []
    for i, cap in enumerate(captions):
        bg = images[i % len(images)]
        out = slides_dir / f"slide_{i:03d}.jpg"
        _render_slide(bg, cap, out)
        slide_paths.append(out)
    if not slide_paths:
        # fallback: single caption-less slide using first image + title
        out = slides_dir / "slide_000.jpg"
        _render_slide(images[0], script.get("title", ""), out)
        slide_paths.append(out)

    segment = duration / len(slide_paths)

    # Build the ffmpeg filter: image sequence with per-segment zoompan + concat,
    # then add audio.
    #
    # Windows path handling for ffmpeg filters (concat demuxer needs forward
    # slashes; drive colon stays unescaped in an ffconcat file).
    def esc(p: Path) -> str:
        return p.as_posix()

    # Use concat demuxer on a list file then process zoompan over the whole thing
    # so one continuous timeline over the concatenated slides, with audio muxed in.
    list_file = run_dir / "slides.txt"
    with open(list_file, "w", encoding="utf-8") as fh:
        for sp in slide_paths:
            fh.write(f"file '{esc(sp)}'\n")

    concat_mp4 = run_dir / "_concat.mp4"
    final = run_dir / inputs["video"]

    # 1) concat slides into a sequence preserving each slide for `segment` seconds.
    # Slides are already rendered at exactly WxH (1080x1920) by _render_slide, so
    # the old per-input scale+letterbox pad is unnecessary (R6) — just keep the
    # sample aspect sane.
    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-fps_mode", "cfr", "-r", str(FPS),
        "-vf", "setsar=1",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(concat_mp4),
    ]
    log.info("Running ffmpeg concat build...")
    _run(cmd_concat)

    # 2) apply a gentle Ken Burns zoompan + add audio
    zoompan = (
        f"zoompan=z='min(zoom+0.0015,1.20)':d={int(segment*FPS)}:"
        f"s={W}x{H}:fps={FPS}"
    )
    cmd_final = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(concat_mp4),
        "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", zoompan,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(final),
    ]
    log.info("Running ffmpeg final render (zoom + audio)...")
    _run(cmd_final)

    if not final.exists():
        raise RuntimeError("FFmpeg assembly produced no output file")

    log.info("Assembled final video: %s (%.1fs, %d slides)",
             final.name, duration, len(slide_paths))
    return {"video": str(final)}
