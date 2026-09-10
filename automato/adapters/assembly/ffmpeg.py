"""Assemble the final 1080x1920 vertical video with local FFmpeg + Pillow.

This stage is pure local computation (not an "external task"), so it runs without
a browser. It:
  1. Renders one captioned slide per caption (background image + text), using the
     channel registry's branding (accent color, preferred font, logo watermark)
     when a channel is set (R8-A3/A5), falling back to the previous generic look
     otherwise.
  2. Times each slide from the TTS stage's word-boundary plan (R8-B2): real
     caption sync when edge-tts served the run, a documented proportional slide
     estimate otherwise. The same timed plan is published as captions.json and
     WebVTT/SRT subtitle files (R8-B6).
  3. Renders each slide as its own Ken Burns clip, concats them with a COPY
     demuxer, and muxes the voiceover -- ducking a background music bed under it
     via a voice keyed sidechain compressor when a licensed track is present
     (R8-B3).
  4. Builds a channel-branded thumbnail (R8-B7) and the publish metadata record
     (R8-B8).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import List

from PIL import Image, ImageDraw, ImageFont

from ... import channels, config
from . import caption_timing

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

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")


def _accent_rgb(value) -> "tuple | None":
    """Parse '#rrggbb' into (r, g, b); None if absent/invalid."""
    if not value:
        return None
    m = _HEX_RE.match(str(value).strip())
    if not m:
        return None
    h = int(m.group(1), 16)
    return (h >> 16 & 255, h >> 8 & 255, h & 255)


def _branding(ctx) -> tuple:
    """(accent_rgb, preferred_font_path, logo_path) from the channel registry.

    ctx may be None (unit tests and stage helpers call run(ctx=None, ...)); every
    lookup here is guarded so branding is a strict no-op upgrade, never a path to
    a crash.
    """
    if ctx is None:
        return (None, None, None)
    try:
        name = ctx.settings.channel_name
    except Exception:  # noqa: BLE001
        return (None, None, None)
    if not name:
        return (None, None, None)
    brand = channels.branding_for(name)
    accent = _accent_rgb(brand.get("accent_color"))
    font_path = None
    logo_path = None
    rel = brand.get("font")
    if rel:
        cand = config.ROOT / str(rel)
        if cand.is_file():
            font_path = cand
    rel = brand.get("logo")
    if rel:
        cand = config.ROOT / str(rel)
        if cand.is_file():
            logo_path = cand
    return (accent, font_path, logo_path)


def _pick_music() -> "Path | None":
    """First licensed background track in the music dir, or None (R8-B3)."""
    if not config.MUSIC_ENABLED or not config.MUSIC_DIR.is_dir():
        return None
    for ext in ("*.mp3", "*.m4a", "*.wav", "*.flac", "*.ogg"):
        for p in sorted(config.MUSIC_DIR.glob(ext)):
            return p
    return None


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


def _load_font(size: int, preferred: "Path | None" = None):
    candidates = ([preferred] if preferred else []) + FONT_CANDIDATES
    for cand in candidates:
        if cand is not None and cand.is_file():
            if cand == _BUNDLED_FONT:
                log.info("No system font found; using bundled DejaVu Sans fallback")
            return ImageFont.truetype(str(cand), size)
    log.warning("No TTF font available at all; using PIL default bitmap font "
                "(low quality captions)")
    return ImageFont.load_default()


def _overlay_logo(im: "Image.Image", logo: "Path | None"):
    """Paste a semi-transparent channel logo in the bottom-right corner (B5)."""
    if not logo or not logo.is_file():
        return
    try:
        mark = Image.open(logo).convert("RGBA")
        mark.thumbnail((160, 160), Image.LANCZOS)
        px, py = 32, H - mark.size[1] - 32
        alpha = mark.split()[3].point(lambda v: int(v * 0.72))
        mark.putalpha(alpha)
        im.paste(mark, (px, py), mark)
        log.info("Watermark logo overlaid: %s", logo.name)
    except Exception as exc:  # noqa: BLE001
        log.warning("Logo overlay skipped (%s)", exc)


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


def _render_slide(bg_path: Path, caption: str, out_path: Path,
                  accent: "tuple | None" = None, font_path: "Path | None" = None,
                  logo_path: "Path | None" = None):
    bg = Image.open(bg_path).convert("RGB")
    bg = bg.resize((W, H), Image.LANCZOS)
    # subtle dark overlay for text contrast
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([0, 0, W, H], fill=(0, 0, 0, 90))
    composite = Image.alpha_composite(bg.convert("RGBA"), overlay)

    draw = ImageDraw.Draw(composite)
    font = _load_font(FONT_SIZE, font_path)
    box_w = W - 120
    box_h = 700
    _draw_text_wrapped(draw, caption, box_w, box_h, font, (255, 255, 255))
    if accent is not None:
        # thin accent bar under the caption block (R8-A3 brand accent)
        bar_y = int(H / 2 + box_h / 2 + 44)
        bar_x = int((W - 260) / 2)
        draw.rectangle([bar_x, bar_y, bar_x + 260, bar_y + 10], fill=accent)
    _overlay_logo(composite, logo_path)
    composite.convert("RGB").save(out_path)
    log.debug("Rendered slide -> %s", out_path.name)


def _render_thumbnail(bg_path: Path, title: str, out_path: Path,
                      accent: "tuple | None" = None,
                      font_path: "Path | None" = None):
    """Channel-branded 1080x1920 thumbnail: darkened image + big title (B7)."""
    bg = Image.open(bg_path).convert("RGB")
    bg = bg.resize((W, H), Image.LANCZOS)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(shade)
    d.rectangle([0, 0, W, H], fill=(0, 0, 0, 70))
    d.rectangle([0, int(H * 0.42), W, H], fill=(0, 0, 0, 150))
    im = Image.alpha_composite(bg.convert("RGBA"), shade)
    draw = ImageDraw.Draw(im)
    font = _load_font(84, font_path)
    box_w = W - 140
    box_h = 560
    _draw_text_wrapped(draw, title, box_w, box_h, font, (255, 255, 255))
    if accent is not None:
        bar_y = int(H * 0.42 + box_h + 56)
        draw.rectangle([(W - 260) // 2, bar_y, (W - 260) // 2 + 260, bar_y + 12],
                       fill=accent)
    im.convert("RGB").save(out_path, quality=90)
    log.info("Thumbnail written -> %s", out_path.name)


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
    narration = script.get("spoken_script", "")

    duration = _probe_duration(audio_path)
    if duration <= 0:
        raise RuntimeError("Could not determine audio duration")

    # R8-B2: plan caption timing from the TTS stage's word-boundary sidecar
    # (real sync when edge-tts served the run, proportional estimate otherwise).
    # Every caption becomes one slide timed to narration.
    timings = caption_timing.load_timings(inputs.get("timings", ""))
    plan = caption_timing.plan_captions(captions, narration, timings,
                                        default_total=duration)
    if not plan and script.get("title"):
        plan = caption_timing.plan_captions([script.get("title")], narration,
                                            timings, default_total=duration)

    accent, font_path, logo_path = _branding(ctx)
    channel = getattr(getattr(ctx, "settings", None), "channel_name", None) if ctx else None

    # 1) Render captioned slides (one per timed caption segment).
    slides_dir = run_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    slide_paths = []
    for seg in plan:
        bg = images[seg["index"] % len(images)]
        out = slides_dir / f"slide_{seg['index']:03d}.jpg"
        _render_slide(bg, seg["text"], out, accent, font_path, logo_path)
        slide_paths.append(out)

    # 2) Render each slide as its own Ken Burns clip; per-segment timing means
    #    the zoom animation restarts with each caption, not one continuous pan.
    segments_dir = run_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    seg_paths = []
    for i, seg in enumerate(plan):
        span = max(seg["end_s"] - seg["start_s"], 0.1)
        frames = max(1, round(span * FPS))
        out = segments_dir / f"seg_{i:03d}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(FPS), "-t", f"{span:.3f}",
            "-i", str(slide_paths[i]),
            "-vf",
            f"zoompan=z='min(zoom+0.0015,1.20)':d={frames}:"
            f"s={W}x{H}:fps={FPS}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
            "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-pix_fmt", "yuv420p",
            str(out),
        ]
        log.info("Rendering slide segment %d/%d (%.2fs)...", i + 1, len(plan), span)
        _run(cmd)
        seg_paths.append(out)

    # 3) Losslessly concat the segments, then mux audio.
    list_file = run_dir / "segments.txt"
    with open(list_file, "w", encoding="utf-8") as fh:
        for sp in seg_paths:
            fh.write(f"file '{sp.as_posix()}'\n")
    concat_mp4 = run_dir / "_concat.mp4"
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
          "-c", "copy", str(concat_mp4)])

    final = run_dir / inputs["video"]
    sfx_plan_ = _sfx_plan(run_dir, plan)
    music = _pick_music()
    if music is not None:
        _mux_final(concat_mp4, audio_path, final, duration, music=music,
                   sfx=sfx_plan_)
        log.info("Muxed audio with ducked music bed (%s)", music.name)
    elif sfx_plan_:
        _mux_final(concat_mp4, audio_path, final, duration, sfx=sfx_plan_)
        log.info("Muxed audio with %d slide SFX", len(sfx_plan_))
    else:
        _mux_voiceonly(concat_mp4, audio_path, final)

    if not final.exists():
        raise RuntimeError("FFmpeg assembly produced no output file")

    # 4) Side artifacts: captions (timed) + subtitle files (B2/B6), brand
    #    thumbnail (B7) and publish metadata (B8). These are bound in the
    #    workflow manifest; publish reads them for description/tags/thumbnail.
    captions_out = run_dir / "captions.json"
    captions_out.write_text(json.dumps({
        "captions": plan,
        "timing_source": timings.get("provider"),
        "all_estimated": all(seg.get("estimated") for seg in plan),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    vtt_out = run_dir / "captions.vtt"
    srt_out = run_dir / "captions.srt"
    caption_timing.write_vtt(plan, vtt_out)
    caption_timing.write_srt(plan, srt_out)

    thumbnail_path = run_dir / "thumbnail.jpg"
    bg_for_thumb = images[0]
    _render_thumbnail(bg_for_thumb, script.get("title") or captions[0] if captions
                      else "Short", thumbnail_path, accent, font_path)

    from ...metadata import build_metadata, write_metadata
    meta = build_metadata(script.get("title", ""), plan,
                          topic=script.get("topic", ""), channel=channel)
    metadata_path = write_metadata(meta, run_dir / "metadata.json")

    log.info("Assembled final video: %s (%.1fs, %d segments)",
             final.name, duration, len(seg_paths))
    return {
        "video": str(final),
        "captions": str(captions_out),
        "subtitle_vtt": str(vtt_out),
        "subtitle_srt": str(srt_out),
        "thumbnail": str(thumbnail_path),
        "metadata": str(metadata_path),
    }


def _mux_voiceonly(video: Path, audio: Path, out: Path):
    return _run([
        "ffmpeg", "-y",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        "-movflags", "+faststart",
        str(out),
    ])


def _sfx_plan(run_dir: Path, plan: List[dict]) -> list:
    """Slide-transition sound effects (R8-B4).

    Picks per-slide SFX from ``config.SFX_DIR``: prefer ``sfx_<index>.{mp3,wav}``
    names, else cycle the whole folder's files in slide order. Returns a list of
    ``(start_ms, path)`` mixed at each slide's start time. Empty when no SFX
    assets exist or the plan is empty.
    """
    if not config.SFX_DIR.is_dir():
        return []
    files = []
    for ext in ("*.mp3", "*.wav", "*.m4a", "*.ogg"):
        files.extend(sorted(config.SFX_DIR.glob(ext)))
    if not files:
        return []
    items = []
    for i, seg in enumerate(plan):
        named = config.SFX_DIR / f"sfx_{i}.mp3"
        if not named.is_file():
            named = config.SFX_DIR / f"sfx_{i}.wav"
        chosen = named if named.is_file() else files[i % len(files)]
        items.append((int(float(seg.get("start_s", 0.0)) * 1000), chosen))
    return items


def _mux_final(video: Path, voice: Path, out: Path, duration: float,
               music: "Path | None" = None, sfx: list = ()) -> None:
    """Mux the voiceover (primary) with optional ducked music + slide SFX.

    R8-B3: when a music bed is present, a sidechain compressor keyed on the
    voiceover ducks the bed so music never fights the words (amix divides inputs
    by count, so volume=M restores unity gain). R8-B4: SFX clips are delayed to
    each slide's start time and mixed in at the same distance from the narration.
    """
    idx = 1
    indexes = []
    parts = [f"[0:a]atrim=0:{duration:.4f},asetpts=PTS-STARTPTS[vo]"]
    if music is not None:
        parts.append(
            f"[{idx}:a]aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={config.MUSIC_BED_LEVEL}[mb]")
        parts.append(
            f"[mb][vo]sidechaincompress=threshold=0.03:"
            f"ratio={config.MUSIC_DUCK_RATIO}:"
            f"attack={config.MUSIC_DUCK_ATTACK_MS}:"
            f"release={config.MUSIC_DUCK_RELEASE_MS}[duck]")
        indexes.append("[duck]")
        idx += 1
    sfx_labels = []
    for k, (start_ms, sfile) in enumerate(sfx):
        parts.append(f"[{idx}:a]aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"volume=0.9,adelay={start_ms}|{start_ms}[sfx{k}]")
        sfx_labels.append(f"[sfx{k}]")
        idx += 1
    sources = "[vo]" + "".join(indexes) + "".join(sfx_labels)
    count = 1 + len(indexes) + len(sfx_labels)
    parts.append(f"{sources}amix=inputs={count}:duration=first:dropout_transition=0,"
                 f"volume={count}[aout]")
    video_idx = idx
    cmd = ["ffmpeg", "-y", "-i", str(voice)]
    if music is not None:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
    for _start_ms, sfile in sfx:
        cmd += ["-i", str(sfile)]
    cmd += ["-i", str(video)]
    cmd += ["-map", f"{video_idx}:v:0", "-map", "[aout]",
            "-filter_complex", ";".join(parts),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
            "-movflags", "+faststart", str(out)]
    return _run(cmd)
