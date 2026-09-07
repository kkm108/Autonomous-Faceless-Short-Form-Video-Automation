"""R2-W1/R2-F1: cross-platform ffmpeg/assembly stage (font fallback, render)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageFont

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
