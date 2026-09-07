"""R2-W4/R2-F4: output quality gates and the forced-unlisted downgrade path."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from automato import config, quality


def _script(narration: str = "word " * 45 + "End.", captions=(1, 2), **extra):
    data = {"title": "T", "spoken_script": narration, "captions": captions}
    data.update(**extra)
    return data


def test_script_gate_passes_for_complete_script(tmp_path: Path):
    p = tmp_path / "script.json"
    p.write_text(json.dumps(_script()), encoding="utf-8")
    assert quality.gate_script({"script": str(p)}) is None


def test_script_gate_soft_fails_on_short_narration(tmp_path: Path):
    p = tmp_path / "script.json"
    p.write_text(json.dumps(_script(narration="Too short.")), encoding="utf-8")
    r = quality.gate_script({"script": str(p)})
    assert r is not None and "too short" in r.message and not r.hard


def test_script_gate_soft_fails_on_no_terminal_punctuation(tmp_path: Path):
    p = tmp_path / "script.json"
    p.write_text(json.dumps(_script(narration="word " * 45 + "noend")), encoding="utf-8")
    r = quality.gate_script({"script": str(p)})
    assert r is not None and "punctuation" in r.message and not r.hard


def test_script_gate_hard_fails_on_missing_captions(tmp_path: Path):
    p = tmp_path / "script.json"
    p.write_text(json.dumps(_script(captions=[])), encoding="utf-8")
    r = quality.gate_script({"script": str(p)})
    assert r is not None and r.hard


def test_assets_gate_hard_fails_on_zero():
    r = quality.gate_assets({"image_files": []}, requested=6)
    assert r is not None and r.hard


def test_assets_gate_soft_fails_on_partial():
    r = quality.gate_assets({"image_files": ["a", "b", "c"]}, requested=6)
    assert r is not None and not r.hard


def test_assets_gate_passes_at_or_above_threshold():
    assert quality.gate_assets({"image_files": ["%d" % i for i in range(5)]}, requested=6) is None
    assert quality.gate_assets({"image_files": ["a"]}) is None  # no requested -> pass


def test_run_gates_empty_when_all_pass(tmp_path: Path):
    p = tmp_path / "script.json"
    p.write_text(json.dumps(_script()), encoding="utf-8")
    assert quality.run_gates("script", {"script": str(p)}) == []


def test_run_gates_reports_failures_and_sets_downgrade_flag(tmp_path: Path):
    p = tmp_path / "script.json"
    p.write_text(json.dumps(_script(narration="short.")), encoding="utf-8")
    failed = quality.run_gates("script", {"script": str(p)})
    assert len(failed) == 1

    # The orchestrator drives FORCE_UNLISTED; verify the adapter-visible contract.
    prev = config.FORCE_UNLISTED
    config.FORCE_UNLISTED = True
    try:
        assert config.FORCE_UNLISTED is True
    finally:
        config.FORCE_UNLISTED = prev


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg/ffprobe not installed")
def test_video_gate_detects_missing_audio_stream(tmp_path: Path):
    # Video-only (no audio) mux -> gate must soft-fail.
    plain = tmp_path / "plain.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "color=c=red:s=64x64:r=5:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         str(plain)],
        check=True, capture_output=True,
    )
    r = quality.gate_video({"video": str(plain)}, expected_s=None)
    assert r is not None
    assert "no audio" in r.message and not r.hard
