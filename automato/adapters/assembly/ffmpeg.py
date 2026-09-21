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

import hashlib
import json
import logging
import random
import re
import subprocess
from pathlib import Path
from typing import List

from PIL import Image, ImageDraw, ImageFont

from ... import ab_test, channels, config
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


# R10-P0 caption-clipping fix: an unbreakable token wider than the caption box
# used to be emitted as its own line, drawn centered at a negative x, and clipped
# at the frame edge (reproduced: a 3727px line in a 960px box). Unbreakable
# tokens are now hard-broken across lines and the wrapped block is scaled down
# until it fits, so a published caption can never leave the safe area. The box
# floor keeps even pathological tokens legible.
#
# R11-F1 follow-up: the fit/placement still measured only the glyph ADVANCE width
# (``draw.textlength``), which ignores the ink that stretches LEFT of the pen
# origin (negative left-side bearing) and the 4px drop shadow. A line whose
# advance just filled the box was therefore centred at x≈0 and its ink landed
# ~2px from the frame edge (reproduced live: 'Why is your keyboard' at 2.0px in
# the keyboard video). Fitting now measures the true ink BOUNDING BOX and reserves
# a widened margin on both sides, and each line is centred by its ink rather than
# its advance, so published captions keep a real safety gap from the frame edges.
_MIN_FONT_SIZE = 28
_FIT_SCALES = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.42, 0.35)
# Widened safety margin each side (the caption box previously implied a 60px
# margin via ``W - 120``; the ink-plus-stroke guard reserves 72px so a near-full
# line cannot reach the edge). ``CAPTION_FLOOR_X`` is the absolute floor that even
# the min-font escape hatch clamps to (a pathological line can never be clipped).
CAPTION_MARGIN_X = 72
CAPTION_FLOOR_X = 8
_SHADOW_OFFSET = 4


def _fit_blank(font, box_w, box_h) -> dict:
    return {"lines": 0, "max_line_px": 0.0, "box_w": box_w, "block_px": 0.0,
            "box_h": box_h, "font_size": font.size, "fit": True}


def _wrap_lines(draw, text: str, box_w, font) -> List[str]:
    """Word-wrap `text` to `box_w`, hard-breaking unbreakable tokens (a slug, a
    hyphenated run, a URL) across lines so no single line can exceed the box."""
    lines: List[str] = []
    for w in text.split():
        if draw.textlength(w, font=font) > box_w:
            piece = ""
            for ch in w:
                if piece and draw.textlength(piece + ch, font=font) > box_w:
                    lines.append(piece)
                    piece = ch
                else:
                    piece += ch
            if piece:
                lines.append(piece)
        elif lines and draw.textlength(lines[-1] + " " + w, font=font) <= box_w:
            lines[-1] += " " + w
        else:
            lines.append(w)
    return lines


def _truncate_lines(draw, lines: List[str], box_w, font, max_lines: int) -> List[str]:
    """Cap the block to ``max_lines`` keeping the *start* (a published caption
    never begins mid-sentence) and signalling the dropped tail with an ellipsis."""
    truncated = len(lines) > max_lines
    while len(lines) > max_lines:
        lines.pop()
    ell = "\u2026"
    if truncated and lines:
        while lines and draw.textlength(lines[-1] + ell, font=font) > box_w:
            lines[-1] = lines[-1][:-1].rstrip()
            if not lines[-1]:
                lines.pop()
                break
    if truncated and lines and not lines[-1].endswith(ell):
        lines[-1] += ell
    return lines


def _shrink_font(font, scale):
    """A copy of ``font`` at ``scale`` (same face); the original when the face
    cannot be re-sized (PIL bitmap default)."""
    path = getattr(font, "path", None)
    if not path:
        return font
    try:
        return ImageFont.truetype(str(path), max(1, int(font.size * scale)))
    except Exception:  # noqa: BLE001
        return font


def _line_ink(draw, ln: str, font) -> tuple:
    """(left, right) ink extent of a line's glyphs relative to the pen origin,
    from the glyph BOUNDING BOX. ``textlength`` only reports the ADVANCE width,
    so a glyph whose ink hangs left of the pen origin (negative left-side bearing,
    heavy strokes, or the drop shadow) could previously push past a frame edge
    even when the advance said the line fit (R11-F1)."""
    left, _top, right, _bottom = draw.textbbox((0, 0), ln, font=font)
    return left, right


def _fit_metrics(draw, font, lines: List[str], box_w, box_h) -> dict:
    """Rendered-fit facts for the R10 assembly self-check: does the wrapped block
    actually sit inside the box's safe area before compositing?

    R11-F1: the fit gate now measures the true INK width of the widest line and
    requires it to stay inside the frame with the widened CAPTION_MARGIN_X on
    each side (the old gate only compared the glyph-advance width against the box,
    which let ink reach ~2px from the frame edge). ``max_line_px`` is kept so the
    advance-based wrap/box checks and their tests remain intact.
    """
    widths = [draw.textlength(ln, font=font) for ln in lines] or [0.0]
    inks = [_line_ink(draw, ln, font) for ln in lines]
    ink_width = max((r - left) for left, r in inks) if inks else 0.0
    line_h = font.size * 1.15
    block_px = line_h * len(lines)
    return {
        "lines": len(lines),
        "max_line_px": max(widths),
        "max_ink_px": round(ink_width, 1),
        "margin_x": CAPTION_MARGIN_X,
        "box_w": box_w,
        "block_px": block_px,
        "box_h": box_h,
        "font_size": font.size,
        "fit": ink_width + 2 * CAPTION_MARGIN_X <= W and block_px <= box_h,
    }


def _draw_text_wrapped(draw, text, box_w, box_h, font, fill) -> dict:
    """Center-and-wrap `text` into the given box, splitting on spaces.

    Over-long captions keep the *start* (so a published caption never begins
    mid-sentence) and drop overflowing trailing lines, signalling truncation with
    a trailing ellipsis.

    R10-P0: text is guaranteed inside the safe area — unbreakable tokens wider
    than the box are hard-broken across lines, and the wrapped block is re-wrapped
    on a scaling ladder until it fits the box. Returns the rendered-fit metrics so
    the assembly self-check can record them (R10-P3).
    """
    if not text.split():
        return _fit_blank(font, box_w, box_h)
    applied = font
    lines: List[str] = []
    for scale in _FIT_SCALES:
        applied = _shrink_font(font, scale)
        lines = _truncate_lines(draw, _wrap_lines(draw, text, box_w, applied),
                                box_w, applied, CAPTION_STYLE["lines"])
        if lines and (_fit_metrics(draw, applied, lines, box_w, box_h)["fit"]
                      or applied.size <= _MIN_FONT_SIZE):
            break
    if not lines:
        return _fit_blank(font, box_w, box_h)
    line_h = applied.size * 1.15
    total_h = line_h * len(lines)
    y = (box_h - total_h) / 2
    for ln in lines:
        l_ink, r_ink = _line_ink(draw, ln, applied)
        ink_w = r_ink - l_ink
        # center the INK (not the advance) so a leading-bearing glyph can never
        # punch past a frame edge, then clamp each edge to the absolute floor the
        # min-font escape hatch is allowed to use (R11-F1).
        x = (W - ink_w) / 2 - l_ink
        if x + l_ink < CAPTION_FLOOR_X:
            x = CAPTION_FLOOR_X - l_ink
        if x + r_ink > W - CAPTION_FLOOR_X:
            x = W - CAPTION_FLOOR_X - r_ink
        # soft shadow for readability
        draw.text((x + _SHADOW_OFFSET, y + _SHADOW_OFFSET), ln, font=applied,
                  fill=(0, 0, 0, 180))
        draw.text((x, y), ln, font=applied, fill=fill)
        y += line_h
    return _fit_metrics(draw, applied, lines, box_w, box_h)


# R11-F1: an asset from a provider with a different native aspect (Perchance
# 512x768 / 768x768, pollinations' letterboxed 1080x1920) sometimes carries a
# BAKED black letterbox/pillarbox bar already in its pixels. _render_slide's
# resize-to-fill runs on every image regardless of source, but a stretch cannot
# remove a bar that is part of the image -- it just scales it. Before filling,
# near-uniform, near-black edge bands are cropped so the rendered slide actually
# covers the frame. Thresholds stay conservative (a genuinely dark photograph has
# more variance and hot pixels than a constant bar) and each side is capped so a
# whole-frame-dark asset is never gutted.
_CROP_BAND_MAX_FRACTION = 1 / 6
_CROP_BLACK_MEAN = 34
_CROP_BLACK_STD = 5.0
_CROP_BLACK_MAX = 70
_CROP_BLACK_FRACTION = 0.95


def _gray_median(gray) -> int:
    hist = gray.histogram()
    total = sum(hist)
    half = total // 2
    acc = 0
    for idx, n in enumerate(hist):
        acc += n
        if acc >= half:
            return int(idx)
    return 255


def _bar_strip_like(strip, median) -> bool:
    """Is a 1px edge strip a near-uniform, near-black bar (versus dark content)?
    Uses the same PIL primitives as the tests (no numpy dependency). Besides the
    near-black/uniform checks, the strip must be MEANINGFULLY DARKER THAN THE
    FRAME MEDIAN: a letterbox bar is always darker than the picture next to it,
    whereas a uniformly dark (noise-only) frame would otherwise match every
    check and get its edges gutted."""
    gray = strip.convert("L")
    hist = gray.histogram()
    total = max(sum(hist), 1)
    fraction_black = sum(hist[: int(_CROP_BLACK_MEAN) + 1]) / total
    if fraction_black < _CROP_BLACK_FRACTION:
        return False
    lo, hi = gray.getextrema()
    if hi > _CROP_BLACK_MAX:
        return False
    mean = sum(i * n for i, n in enumerate(hist)) / total
    if mean + 8 >= median:
        return False
    from PIL import ImageStat
    std = ImageStat.Stat(gray).stddev[0]
    return std <= _CROP_BLACK_STD


def _black_edge_band(gray, dim: int, edge) -> int:
    """Max run of consecutive bar-like 1px strips inward from `edge` ('top',
    'bottom', 'left', 'right'), capped per side. Walk stops at the first strip
    that is not bar-like, so a real bar (which has content after it) is removed
    while a uniformly dark frame (no bar-vs-content contrast) leaves its edges
    untouched."""
    median = _gray_median(gray)
    cap = max(1, int(dim * _CROP_BAND_MAX_FRACTION))
    run = 0
    for o in range(cap):
        coord = o if edge in ("top", "left") else dim - 1 - o
        if edge in ("top", "bottom"):
            strip = gray.crop((0, coord, gray.width, coord + 1))
        else:
            strip = gray.crop((coord, 0, coord + 1, gray.height))
        if not _bar_strip_like(strip, median):
            break
        run += 1
    return run


def _crop_black_margins(im) -> "Image.Image":
    """Crop baked black letterbox/pillarbox bands off every edge (R11-F1)."""
    original = im.size
    gray = im.convert("L")
    top = _black_edge_band(gray, gray.height, "top")
    bottom = _black_edge_band(gray, gray.height, "bottom")
    left = _black_edge_band(gray, gray.width, "left")
    right = _black_edge_band(gray, gray.width, "right")
    box = (left, top, im.size[0] - right, im.size[1] - bottom)
    if box[:2] == (0, 0) and box[2:] == original:
        return im
    return im.crop(box)


def _fill_frame(bg) -> "Image.Image":
    """Foreground-fill `bg` to the exact 1080x1920 frame, cropping baked black
    letterbox bars before the resize (R11-F1)."""
    return _crop_black_margins(bg).resize((W, H), Image.LANCZOS)


def _render_slide(bg_path: Path, caption: str, out_path: Path,
                  accent: "tuple | None" = None, font_path: "Path | None" = None,
                  logo_path: "Path | None" = None):
    bg = Image.open(bg_path).convert("RGB")
    bg = _fill_frame(bg)
    # subtle dark overlay for text contrast
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([0, 0, W, H], fill=(0, 0, 0, 90))
    composite = Image.alpha_composite(bg.convert("RGBA"), overlay)

    draw = ImageDraw.Draw(composite)
    font = _load_font(FONT_SIZE, font_path)
    box_w = W - 120
    box_h = 700
    fit = _draw_text_wrapped(draw, caption, box_w, box_h, font, (255, 255, 255))
    if not fit["fit"]:
        log.warning("Caption did not fit safe area on %s: %s", out_path.name, fit)
    if accent is not None:
        # thin accent bar under the caption block (R8-A3 brand accent)
        bar_y = int(H / 2 + box_h / 2 + 44)
        bar_x = int((W - 260) / 2)
        draw.rectangle([bar_x, bar_y, bar_x + 260, bar_y + 10], fill=accent)
    _overlay_logo(composite, logo_path)
    composite.convert("RGB").save(out_path)
    log.debug("Rendered slide -> %s", out_path.name)
    return fit


def _images_distinct(images: List[Path]) -> bool:
    """R10-P0/P3: every slide image in one video must be content-distinct.
    A reused/fallback duplicate is exactly the image/mismatch defect the last
    review found; this is the mechanical per-video check that would have caught
    it (the assets quality gate enforces it as a soft-fail before assembly)."""
    seen: set = set()
    distinct = True
    for p in images:
        try:
            h = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue
        if h in seen:
            distinct = False
        seen.add(h)
    return distinct


def _render_thumbnail(bg_path: Path, title: str, out_path: Path,
                      accent: "tuple | None" = None,
                      font_path: "Path | None" = None):
    """Channel-branded 1080x1920 thumbnail: darkened image + big title (B7)."""
    bg = Image.open(bg_path).convert("RGB")
    bg = _fill_frame(bg)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(shade)
    d.rectangle([0, 0, W, H], fill=(0, 0, 0, 70))
    d.rectangle([0, int(H * 0.42), W, H], fill=(0, 0, 0, 150))
    im = Image.alpha_composite(bg.convert("RGBA"), shade)
    draw = ImageDraw.Draw(im)
    font = _load_font(84, font_path)
    box_w = W - 140
    box_h = 560
    fit = _draw_text_wrapped(draw, title, box_w, box_h, font, (255, 255, 255))
    if not fit["fit"]:
        log.warning("Thumbnail title did not fit safe area: %s", fit)
    if accent is not None:
        bar_y = int(H * 0.42 + box_h + 56)
        draw.rectangle([(W - 260) // 2, bar_y, (W - 260) // 2 + 260, bar_y + 12],
                       fill=accent)
    im.convert("RGB").save(out_path, quality=90)
    log.info("Thumbnail written -> %s", out_path.name)
    return fit


def _probe_duration(path: Path) -> float:
    proc = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
    )
    try:
        return float(proc.stdout.strip())
    except Exception:  # noqa: BLE001
        return 0.0


# R11-F3 Ken Burns variety: the single fixed zoom-in preset is replaced by a small
# rotating set picked PER SLIDE through the same weighted/pinnable mechanism the
# provider cascade uses (ab_test.lead_with: AUTOMATO_MOTION_WEIGHTS env weights,
# AUTOMATO_MOTION_PIN override, deterministic per-segment seed). Scope is motion /
# pacing ONLY: font, colour and caption position stay governed by the channel
# branding profile (R8). Expressions are baked with the real frame count and the
# chosen preset is recorded to a motion.json sidecar (kept out of
# provider_choices.json so the batch A/B analytics stay clean).
MOTION_PRESETS = {
    "zoom_in": {
        "z": "min(zoom+0.0015,1.20)",
        "x": "iw/2-(iw/zoom/2)",
        "y": "ih/2-(ih/zoom/2)",
        "label": "slow zoom in",
    },
    "zoom_out": {
        "z": "max(1.20-(on-1)*0.0015,1.0)",
        "x": "iw/2-(iw/zoom/2)",
        "y": "ih/2-(ih/zoom/2)",
        "label": "slow zoom out",
    },
    "pan_left": {
        "z": "1.18",
        "x": "(iw-(iw/zoom))*(1-((on-1)/{den}))",
        "y": "ih/2-(ih/zoom/2)",
        "label": "pan left",
    },
    "pan_right": {
        "z": "1.18",
        "x": "(iw-(iw/zoom))*((on-1)/{den})",
        "y": "ih/2-(ih/zoom/2)",
        "label": "pan right",
    },
    "drift": {
        "z": "1.08",
        "x": "iw/2-(iw/zoom/2)",
        "y": "(ih-(ih/zoom))*(({frames}-on)/{den})",
        "label": "subtle static drift",
    },
}
MOTION_CHAIN = list(MOTION_PRESETS)


def _zoompan_filter(name: str, frames: int) -> str:
    """The zoompan -vf expression for a motion preset, over ``frames`` output
    frames. Expressions are baked with the real per-segment frame count (never
    the ``duration`` variable) so each preset is deterministic per segment."""
    preset = MOTION_PRESETS[name]
    den = max(1, frames - 1)
    z = preset["z"].format(frames=frames, den=den)
    x = preset["x"].format(frames=frames, den=den)
    y = preset["y"].format(frames=frames, den=den)
    return (f"zoompan=z='{z}':d={frames}:s={W}x{H}:fps={FPS}:"
            f"x='{x}':y='{y}'")


def _motion_pick(seed_ref, index: int) -> tuple:
    """(preset name, chosen_by label) for one segment, seeded from a stable key so
    a resumed run reproduces its motion plan. Production passes the run dir name
    (deterministic per run); unit tests pass any stable key."""
    rng = None
    if seed_ref is not None:
        rng = random.Random(f"motion:{seed_ref}:{index}")
    _rotated, head, chosen_by = ab_test.lead_with("motion", MOTION_CHAIN, rng=rng)
    return head, chosen_by


def _write_motion_sidecar(run_dir: Path, choices: list) -> Path:
    """Record the per-slide motion plan (R11-F3 audit trail). Deliberately kept
    out of provider_choices.json: the batch A/B correlation reads that file across
    all stages generically, and motion is presentation, not rendering choice."""
    out = run_dir / "motion.json"
    out.write_text(json.dumps({
        "stage": "motion",
        "weights": ab_test.weights_for("motion"),
        "pin": ab_test.pinned_for("motion"),
        "per_segment": choices,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


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
    fit_records = []
    for seg in plan:
        bg = images[seg["index"] % len(images)]
        out = slides_dir / f"slide_{seg['index']:03d}.jpg"
        fit = _render_slide(bg, seg["text"], out, accent, font_path, logo_path)
        slide_paths.append(out)
        fit_records.append({"slide": seg["index"], "text": seg["text"][:60], **fit})

    # 2) Render each slide as its own Ken Burns clip; per-segment timing means
    #    the zoom animation restarts with each caption, not one continuous pan.
    #    R11-F3: motion style rotates through the preset set per slide instead of
    #    the former single zoom-in, driven by the same weights/pin mechanics.
    segments_dir = run_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    seg_paths = []
    motion_choices = []
    motion_seed = run_dir.name
    for i, seg in enumerate(plan):
        span = max(seg["end_s"] - seg["start_s"], 0.1)
        frames = max(1, round(span * FPS))
        out = segments_dir / f"seg_{i:03d}.mp4"
        motion, chosen_by = _motion_pick(motion_seed, i)
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(FPS), "-t", f"{span:.3f}",
            "-i", str(slide_paths[i]),
            "-vf", _zoompan_filter(motion, frames),
            "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-pix_fmt", "yuv420p",
            str(out),
        ]
        log.info("Rendering slide segment %d/%d (%.2fs, motion=%s)...",
                 i + 1, len(plan), span, motion)
        _run(cmd)
        seg_paths.append(out)
        motion_choices.append({"slide": i, "preset": motion,
                               "label": MOTION_PRESETS[motion]["label"],
                               "chosen_by": chosen_by})
    motion_path = _write_motion_sidecar(run_dir, motion_choices)

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
    thumb_title = script.get("title") or captions[0] if captions else "Short"
    thumb_fit = _render_thumbnail(bg_for_thumb, thumb_title, thumbnail_path,
                                  accent, font_path)

    from ...metadata import build_metadata, write_metadata
    tier = channels.risk_tier_for(channel) if channel else ""
    meta = build_metadata(script.get("title", ""), plan,
                          topic=script.get("topic", ""), channel=channel,
                          risk_tier=tier)
    metadata_path = write_metadata(meta, run_dir / "metadata.json")

    # R10-P3 assembly self-check: record every caption/thumbnail render-fit (and
    # the per-video image-distinctness fact) so the gate evidence survives next to
    # the video. The render code already guarantees fit; this is the audit trail.
    fit_path = run_dir / "caption_fit.json"
    fit_records.append({"slide": "thumbnail", "text": thumb_title[:60], **thumb_fit})
    fit_path.write_text(json.dumps({
        "records": fit_records,
        "all_fit": all(r.get("fit") for r in fit_records),
        "image_hashes_distinct": _images_distinct(images),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if not all(r.get("fit") for r in fit_records):
        log.warning("Captions.json sidecar records an out-of-box render: %s",
                    [r for r in fit_records if not r.get("fit")])

    log.info("Assembled final video: %s (%.1fs, %d segments)",
             final.name, duration, len(seg_paths))
    return {
        "video": str(final),
        "captions": str(captions_out),
        "subtitle_vtt": str(vtt_out),
        "subtitle_srt": str(srt_out),
        "thumbnail": str(thumbnail_path),
        "metadata": str(metadata_path),
        "caption_fit": str(fit_path),
        "motion": str(motion_path),
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
