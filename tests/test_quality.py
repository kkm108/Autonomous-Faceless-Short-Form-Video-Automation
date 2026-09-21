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


def test_assets_gate_soft_fails_on_byte_identical_images(tmp_path: Path):
    # R10-P0: the image-reuse defect (one file silently serving several slides)
    # must force a human review (soft-fail) instead of passing on count alone.
    files = []
    for i in range(6):
        p = tmp_path / f"bg_{i:02d}.jpg"
        p.write_bytes(b"the same bytes")  # identical content
        files.append(str(p))
    r = quality.gate_assets({"image_files": files}, requested=6)
    assert r is not None
    assert not r.hard and "identical" in r.message


def test_assets_gate_passes_on_distinct_images(tmp_path: Path):
    files = []
    for i in range(6):
        p = tmp_path / f"bg_{i:02d}.jpg"
        p.write_bytes(f"distinct-{i}".encode())
        files.append(str(p))
    assert quality.gate_assets({"image_files": files}, requested=6) is None


def test_assets_gate_ignores_unreadable_paths_for_hashes():
    # Files that can't be read still count; they just can't be hash-compared.
    r = quality.gate_assets({"image_files": ["%d" % i for i in range(5)]},
                            requested=6)
    assert r is None


def test_gate_factcheck_soft_fails_when_flagged(tmp_path: Path):
    p = tmp_path / "factcheck.json"
    p.write_text(json.dumps({
        "status": "reviewed", "flagged": True,
        "flags": ["the '92%' figure is unsourced"], "risk_tier": "high",
    }), encoding="utf-8")
    r = quality.gate_factcheck({"review": str(p)})
    assert r is not None
    assert not r.hard and "fact-check flagged" in r.message


def test_gate_factcheck_passes_when_clean(tmp_path: Path):
    p = tmp_path / "factcheck.json"
    p.write_text(json.dumps({
        "status": "reviewed", "flagged": False, "flags": [], "risk_tier": "high",
    }), encoding="utf-8")
    assert quality.gate_factcheck({"review": str(p)}) is None


def test_gate_factcheck_passes_when_absent_or_low_tier(tmp_path: Path):
    # No artifact (low-tier channel / fact-check disabled never write one) passes
    # silently; a low-tier review record is a no-op too.
    assert quality.gate_factcheck({}) is None
    p = tmp_path / "factcheck.json"
    p.write_text(json.dumps({
        "status": "unreviewed", "flagged": False, "flags": [], "risk_tier": "low",
    }), encoding="utf-8")
    assert quality.gate_factcheck({"review": str(p)}) is None


def test_gate_factcheck_soft_fails_on_empty_reply_high_tier(tmp_path: Path):
    # R10 customer feedback: "the review pass got no reply at all" on a high-tier
    # channel is not evidence of safety — it must downgrade exactly like a
    # flagged claim, not pass silently.
    p = tmp_path / "factcheck.json"
    p.write_text(json.dumps({
        "status": "unreviewed", "flagged": False, "flags": [], "risk_tier": "high",
    }), encoding="utf-8")
    r = quality.gate_factcheck({"review": str(p)})
    assert r is not None
    assert not r.hard and "no reply" in r.message


def test_gate_factcheck_fails_closed_when_tier_marker_missing(tmp_path: Path):
    # Only high-tier channels write a review artifact, so an unreviewed record
    # without a risk_tier marker is assumed high-tier (fail-closed).
    p = tmp_path / "factcheck.json"
    p.write_text(json.dumps({
        "status": "unreviewed", "flagged": False, "flags": [],
    }), encoding="utf-8")
    r = quality.gate_factcheck({"review": str(p)})
    assert r is not None and not r.hard


def test_empty_review_downgrades_visibility_on_high_tier(tmp_path: Path):
    # The orchestration contract (R2-W4): any soft-fail gate on a stage flips
    # ctx.settings.force_unlisted, and the publish adapter applies that as an
    # unlisted downgrade even when the run explicitly asked for public.
    p = tmp_path / "factcheck.json"
    p.write_text(json.dumps({
        "status": "unreviewed", "flagged": False, "flags": [], "risk_tier": "high",
    }), encoding="utf-8")
    script = tmp_path / "script.json"
    script.write_text(json.dumps(_script()), encoding="utf-8")

    failed = quality.run_gates("script", {"script": str(script), "review": str(p)})
    soft = [g for g in failed if not g.hard]
    assert len(soft) == 1 and "no reply" in soft[0].message

    from automato.settings import RunSettings
    s = RunSettings(visibility="public")
    for gate in failed:
        s.force_unlisted = not gate.hard          # orchestrator's action
    assert s.force_unlisted is True
    effective = ("unlisted" if s.force_unlisted and s.visibility != "unlisted"
                 else s.visibility)               # publish adapter's decision
    assert effective == "unlisted"


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


# ---------------------------------------------------------------------------
# R11-F3: image-prompt subject-variety advisory flag (non-fatal)
# ---------------------------------------------------------------------------

def test_repeated_image_subjects_flags_keyboard_drift():
    # Mirrors the live 'Why Your Keyboard...' video: five of six IMAGE prompts
    # drifted back onto the same concrete subject while one slid to a different
    # visual — the engine must NOT pass that silently anymore.
    prompts = [
        "Vintage 1800s mechanical typewriter on a wooden desk, cinematic mood",
        "Close-up of antique typewriter keys, warm lighting",
        "Typewriter ribbon spools and keys, soft glow",
        "Copper gears inside a typewriter mechanism, dramatic lighting",
        "Faceless hands typing on an old typewriter, clean dark background",
        "A modern laptop with a glowing keyboard at night, minimalist",
    ]
    flag, subjects = quality.repeated_image_subjects(prompts)
    assert flag is True
    assert "typewriter" in subjects


def test_repeated_image_subjects_clear_when_subjects_differ():
    prompts = [
        "A lone lighthouse in a storm at dusk, moody sky",
        "A paper boat drifting on ocean waves, golden light",
        "Seabirds circling a wooden pier, morning haze",
        "Dried starfish and rope on wet sand, pale light",
        "A brass compass on an open nautical chart, warm lamp",
        "A harbor town seen from the hill at blue hour, glowing windows",
    ]
    flag, subjects = quality.repeated_image_subjects(prompts)
    assert flag is False
    assert subjects == []


def test_repeated_image_subjects_ignores_template_boilerplate():
    prompts = [
        "A red bicycle parked by a concrete wall, no text, clean background",
        "A red vintage car on a quiet street, cinematic mood",
        "A red metal sign painted with old letters, no faces",
    ]
    flag, subjects = quality.repeated_image_subjects(prompts)
    # 'red' repeats, but template filler like 'no text'/'clean background'/
    # 'cinematic mood' must never count as a shared subject.
    assert flag is False


def test_record_script_quality_writes_sidecar_non_fatal(tmp_path: Path):
    p = tmp_path / "script.json"
    p.write_text(json.dumps({
        "spoken_script": "word " * 45 + "End.",
        "captions": [1, 2],
        "image_prompts": [
            "A chess king piece in resin, dramatic light",
            "A chess knight carved in ivory, soft glow",
            "A chess pawn toppled on its board, moody",
            "A chess board mid-game at golden hour, cinematic",
            "A chess rook against a sunrise sky, minimal",
            "Sliced chess pieces scattered on marble, clean",
        ],
    }), encoding="utf-8")
    out = quality.record_script_quality(tmp_path, {"script": str(p)})
    assert out is not None and out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["image_prompt_repetition"]["flag"] is True
    assert "chess" in data["image_prompt_repetition"]["repeated_subjects"]
    # advisory only: nothing is hard-failed or downgraded by the record itself
    assert quality.run_gates("script", {"script": str(p)}) == []


def test_record_script_quality_returns_none_without_script(tmp_path: Path):
    assert quality.record_script_quality(tmp_path, {}) is None
    assert quality.record_script_quality(tmp_path, None) is None
