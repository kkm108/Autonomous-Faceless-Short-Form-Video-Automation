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
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

W, H = 1080, 1920
FPS = 30
FONT_SIZE = 92
CAPTION_STYLE = {
    "lines": 3,
}

# Candidates for a bundable font (we rely on Windows fonts present on the host).
FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
]


def _load_font(size: int):
    import os
    for cand in FONT_CANDIDATES:
        if os.path.exists(cand):
            return ImageFont.truetype(cand, size)
    return ImageFont.load_default()


def _draw_text_wrapped(draw, text, box_w, box_h, font, fill):
    """Center-and-wrap `text` into the given box, splitting on spaces."""
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
    # drop overflowing lines (keep last) - simple heuristic
    while len(lines) > CAPTION_STYLE["lines"]:
        lines.pop(0)
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
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
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

    # 1) concat slides into a sequence preserving each slide for `segment` seconds
    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-fps_mode", "cfr", "-r", str(FPS),
        "-vf", (
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1"
        ),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(concat_mp4),
    ]
    log.info("Running ffmpeg concat build...")
    subprocess.run(cmd_concat, check=True, capture_output=True)

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
    subprocess.run(cmd_final, check=True, capture_output=True)

    if not final.exists():
        raise RuntimeError("FFmpeg assembly produced no output file")

    log.info("Assembled final video: %s (%.1fs, %d slides)",
             final.name, duration, len(slide_paths))
    return {"video": str(final)}
