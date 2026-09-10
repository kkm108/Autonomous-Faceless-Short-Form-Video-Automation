"""R8: caption timing (B2), subtitle writers (B6), metadata (B8) and the
keyless fallback-image degradation gate (B1)."""
import json

from automato import quality
from automato.adapters.assembly import caption_timing


def _sample_timings(words):
    return {
        "provider": "edge_tts",
        "words": [
            {"word": w, "start_s": s, "end_s": s + 0.4}
            for s, w in enumerate(words)
        ],
        "total_s": len(words) * 1.0 + 0.4,
    }


def test_greedy_match_lands_at_real_words():
    timings = _sample_timings(["Hello", "world", "this", "works"])
    plan = caption_timing.plan_captions(["Hello world", "this works"],
                                        "Hello world this works", timings)
    assert [p["text"] for p in plan] == ["Hello world", "this works"]
    assert all(p["estimated"] is False for p in plan)
    assert plan[0]["start_s"] == 0.0
    assert plan[0]["end_s"] == 1.4
    assert plan[1]["start_s"] == 2.0
    assert plan[1]["end_s"] == 3.4
    assert len(plan) == 2


def test_no_timings_falls_back_to_proportional_estimate():
    plan = caption_timing.plan_captions(["One", "two three"],
                                        "one two three", {})
    assert all(p["estimated"] for p in plan)
    # summary: starts at 0, covers total, monotonic.
    assert plan[0]["start_s"] == 0.0
    assert plan[-1]["end_s"] > plan[0]["end_s"]


def test_estimate_anchored_on_default_total():
    plan = caption_timing.plan_captions(["A", "B", "C"], "", {},
                                        default_total=9.0)
    assert abs(plan[-1]["end_s"] - 9.0) < 0.01


def test_srt_and_vtt_writers(tmp_path):
    timings = _sample_timings(["word", "two", "three"])
    plan = caption_timing.plan_captions(["word", "two three"], "word two three",
                                        timings)
    srt = caption_timing.write_srt(plan, tmp_path / "c.srt")
    caption_timing.write_vtt(plan, tmp_path / "c.vtt")
    assert "WEBVTT" in (tmp_path / "c.vtt").read_text(encoding="utf-8")
    assert " --> " in (tmp_path / "c.srt").read_text(encoding="utf-8")
    assert srt.endswith("c.srt")


def test_degraded_assets_are_a_soft_fail():
    gate = quality.gate_assets({"image_files": ["x"] * 6,
                                "degraded_reason": "Perchance fell short (rescue)"},
                               requested=6)
    assert gate is not None
    assert gate.ok is False
    assert gate.hard is False


def test_metadata_round_trip(tmp_path):
    from automato import metadata
    plan = caption_timing.plan_captions(["Intro", "Body", "End"], "intro body end",
                                        {}, default_total=20.0)
    meta = metadata.build_metadata("My Short", plan, topic="robots", channel="main")
    p = metadata.write_metadata(meta, tmp_path / "metadata.json")
    assert json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata.read_metadata(p)["title"] == "My Short"
    assert isinstance(meta["tags"], list) and "shorts" in meta["tags"]


def test_fallback_generate_image_normalizes_to_jpeg(tmp_path, monkeypatch):
    import io

    from PIL import Image

    from automato.adapters.assets import fallback

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buf, "JPEG")
    jpeg_bytes = buf.getvalue()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return jpeg_bytes

    def _fake_urlopen(req, timeout=0):
        return _Resp()

    monkeypatch.setattr(fallback.urllib.request, "urlopen", _fake_urlopen)
    out = tmp_path / "bg_00.jpg"
    path, source = fallback.generate_image("a cat on a server rack", out)
    assert source == "pollinations"
    assert path.exists() and path.stat().st_size > 0
    im = Image.open(path)
    assert im.size == (fallback.W, fallback.H)
