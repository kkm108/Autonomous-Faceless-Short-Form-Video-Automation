"""R2-W1/R2-F1: cross-platform ffmpeg/assembly stage (font fallback, render)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from automato import config
from automato.adapters.assembly import ffmpeg


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def test_load_font_resolves_to_a_truetype_font_on_any_platform():
    # The bundled DejaVu Sans must always be found, so caption rendering never
    # silently degrades to PIL's default bitmap font — this is what makes the
    # local assembly stage work on the Linux CI runner too.
    font = ffmpeg._load_font(ffmpeg.FONT_SIZE)
    assert isinstance(font, ImageFont.FreeTypeFont)
    assert font.size == ffmpeg.FONT_SIZE


def test_render_slide_produces_a_slide(tmp_path: Path):
    bg = Image.new("RGB", (64, 64), (30, 40, 50))
    bg_path = tmp_path / "bg.jpg"
    bg.save(bg_path)
    out = tmp_path / "slide_000.jpg"
    ffmpeg._render_slide(bg_path, "Hello, world.", out)
    assert out.exists()
    assert out.stat().st_size > 0


def test_bundled_font_file_exists_and_is_loadable():
    p = ffmpeg._BUNDLED_FONT
    assert p.is_file()
    ImageFont.truetype(str(p), 20)


# ---------------------------------------------------------------------------
# R11-F1: baked black letterbox bands are cropped before the resize-fill
# ---------------------------------------------------------------------------

def test_crop_black_margins_removes_baked_letterbox_bands():
    # A Perchance 512x768 asset with a baked bottom bar (like the 'Why Your
    # Keyboard...' video's slide 5). The crop is bounded per side (1/6 of the
    # dimension), so a 90px bar on a 420px-tall image sheds exactly the cap.
    im = Image.new("RGB", (300, 420), (120, 90, 60))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 330, 299, 419], fill=(0, 0, 0))   # baked bottom letterbox
    out = ffmpeg._crop_black_margins(im)
    assert out.height == 350                          # bar removed up to the 1/6 cap


def test_crop_black_margins_removes_all_four_edges():
    im = Image.new("RGB", (200, 300), (140, 100, 80))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 199, 24], fill=(0, 0, 0))      # top bar
    d.rectangle([0, 275, 199, 299], fill=(0, 0, 0))   # bottom bar
    d.rectangle([0, 0, 24, 299], fill=(0, 0, 0))      # left bar
    d.rectangle([175, 0, 199, 299], fill=(0, 0, 0))   # right bar
    out = ffmpeg._crop_black_margins(im)
    assert out.size == (150, 250)


def test_crop_black_margins_spares_textured_dark_content():
    # A genuinely dark-but-photographic frame (noise, not a constant bar) must
    # be left untouched — the conservative thresholds are what stop a moody
    # night-shot being gutted.
    im = Image.new("RGB", (200, 300), (4, 5, 6))
    d = ImageDraw.Draw(im)
    for y in range(0, 300, 2):
        d.line([(0, y), (199, y)], fill=(7 + y % 5, 8 + y % 4, 9 + y % 6))
    out = ffmpeg._crop_black_margins(im)
    assert out.size == im.size                          # nothing cropped


def test_fill_frame_returns_exact_1080x1920_after_crop():
    im = Image.new("RGB", (300, 420), (90, 110, 130))
    ImageDraw.Draw(im).rectangle([0, 380, 299, 419], fill=(0, 0, 0))
    out = ffmpeg._fill_frame(im)
    assert out.size == (ffmpeg.W, ffmpeg.H)


# ---------------------------------------------------------------------------
# R11-F3: rotating Ken Burns presets (weighted/pinnable per slide)
# ---------------------------------------------------------------------------

def test_motion_presets_cover_the_documented_set():
    assert set(ffmpeg.MOTION_CHAIN) == set(ffmpeg.MOTION_PRESETS)
    # the chain is the cascade/lead order (dict insertion order), not sorted
    assert ffmpeg.MOTION_CHAIN == list(ffmpeg.MOTION_PRESETS)
    for name, spec in ffmpeg.MOTION_PRESETS.items():
        assert {"z", "x", "y", "label"}.issubset(spec), name


def test_zoompan_filter_bakes_real_frame_count_no_placeholders():
    expr = ffmpeg._zoompan_filter("pan_left", 75)
    assert ":d=75:" in expr
    assert f"s={ffmpeg.W}x{ffmpeg.H}" in expr
    assert f"fps={ffmpeg.FPS}" in expr
    assert "{" not in expr and "duration" not in expr   # baked, deterministic
    assert "zoompan=z='" in expr


def test_motion_pick_is_deterministic_per_seed_and_index():
    assert ffmpeg._motion_pick("run-abc-000", 0) == ffmpeg._motion_pick("run-abc-000", 0)
    assert ffmpeg._motion_pick("run-abc-000", 3) == ffmpeg._motion_pick("run-abc-000", 3)
    # different runs may rotate differently, but always to a known preset
    name, chosen = ffmpeg._motion_pick("other-run", 0)
    assert name in ffmpeg.MOTION_CHAIN
    assert chosen in ("pinned", "weight", "default")


def test_motion_pick_respects_pin(monkeypatch):
    monkeypatch.setenv("AUTOMATO_MOTION_PIN", "drift")
    name, chosen = ffmpeg._motion_pick("run-x", 4)
    assert name == "drift"
    assert chosen == "pinned"


def test_motion_pick_rolls_respects_weights(monkeypatch):
    monkeypatch.delenv("AUTOMATO_MOTION_PIN", raising=False)
    monkeypatch.setenv("AUTOMATO_MOTION_WEIGHTS", "zoom_out:1,zoom_in:100")
    names = [ffmpeg._motion_pick("run-y", i)[0] for i in range(200)]
    assert all(n in ("zoom_in", "zoom_out") for n in names)
    assert names.count("zoom_in") > names.count("zoom_out")  # 100:1 bias holds


def test_motion_weights_default_matches_config(monkeypatch):
    import automato.ab_test as ab_test
    monkeypatch.delenv("AUTOMATO_MOTION_WEIGHTS", raising=False)
    assert ab_test.weights_for("motion") == config.DEFAULT_WEIGHTS["motion"]


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not installed")
def test_assemble_stage_end_to_end(tmp_path: Path):
    """Full local assemble stage on synthetic assets — exercises ffmpeg on any OS
    (this is what runs on the Linux CI runner; no browser or account needed)."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    for i in range(2):
        Image.new("RGB", (64, 64), (20 + i * 40, 30, 40)).save(assets / f"bg_{i:02d}.jpg")

    # 2s of silence for the voiceover.
    audio = tmp_path / "voice.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
         "-t", "2", str(audio)],
        check=True, capture_output=True,
    )

    (tmp_path / "script.json").write_text(
        json.dumps({"title": "T", "captions": ["Hello.", "World."]}), encoding="utf-8")

    inputs = {
        "script": str(tmp_path / "script.json"),
        "audio": str(audio),
        "images": str(assets),
        "video": "final.mp4",
    }
    out = ffmpeg.run(None, inputs, tmp_path, session=None)
    final = Path(out["video"])
    assert final.exists() and final.stat().st_size > 0
    duration = ffmpeg._probe_duration(final)
    assert 1.0 < duration <= 3.0
