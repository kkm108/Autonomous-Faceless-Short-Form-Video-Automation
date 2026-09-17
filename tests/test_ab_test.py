"""R11-W2/W3: weighted+pinnable provider selection, degraded/AB semantics,
voiceover degraded gate, and the analytics correlation layer."""
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from automato import ab_results, ab_test, config
from automato.adapters.assets import perchance_images
from automato.adapters.metrics import studio_metrics
from automato.adapters.scripting import generic_llm
from automato.adapters.tts import kokoro_tts
from automato.quality import gate_degraded, run_gates

# ---------------------------------------------------------------------------
# weight parsing / env
# ---------------------------------------------------------------------------

def test_parse_weights_basic():
    assert ab_test.parse_weights("edge_tts:4,soundtools:1,pyttsx3:1") == {
        "edge_tts": 4, "soundtools": 1, "pyttsx3": 1}


@pytest.mark.parametrize("bad", ["", "  ", "a", "a:", "a:x", "a:-1"])
def test_parse_weights_rejects_garbage(bad):
    with pytest.raises(Exception):
        ab_test.parse_weights(bad)


def test_weights_for_env_override_and_code_default(monkeypatch):
    monkeypatch.setenv("AUTOMATO_TTS_WEIGHTS", "edge_tts:1,soundtools:1")
    assert ab_test.weights_for("tts") == {"edge_tts": 1, "soundtools": 1}
    monkeypatch.delenv("AUTOMATO_TTS_WEIGHTS")
    assert ab_test.weights_for("tts") == config.DEFAULT_WEIGHTS["tts"]
    # a stage with no table falls back to an empty (no-roll) weight set
    assert ab_test.weights_for("nope") == {}


# ---------------------------------------------------------------------------
# lead_with: pinned / weighted / default rotation
# ---------------------------------------------------------------------------

def test_pin_respected_and_bypasses_weights(monkeypatch):
    monkeypatch.setenv("AUTOMATO_TTS_WEIGHTS", "soundtools:100,edge_tts:1")
    monkeypatch.setenv("AUTOMATO_TTS_PIN", "pyttsx3")
    chain, head, chosen_by = ab_test.lead_with("tts",
                                               ["edge_tts", "soundtools", "pyttsx3"])
    assert chosen_by == "pinned"
    assert head == "pyttsx3"
    # pin leads, cascade keeps the ORIGINAL relative order for everything else
    assert chain == ["pyttsx3", "edge_tts", "soundtools"]


def test_pin_not_in_chain_is_ignored(monkeypatch):
    monkeypatch.setenv("AUTOMATO_TTS_PIN", "wat_provider")
    chain, head, chosen_by = ab_test.lead_with("tts",
                                               ["edge_tts", "soundtools", "pyttsx3"])
    assert chosen_by == "weight" or chosen_by == "default"
    assert set(chain) == {"edge_tts", "soundtools", "pyttsx3"}


def test_equal_weights_are_roughly_uniform(monkeypatch):
    monkeypatch.setenv("AUTOMATO_TTS_WEIGHTS", "edge_tts:1,soundtools:1,pyttsx3:1")
    # unroll the single random stream the caller would pass
    rng = random.Random(1234)
    counts = {"edge_tts": 0, "soundtools": 0, "pyttsx3": 0}
    for _ in range(3000):
        chain, head, _cb = ab_test.lead_with(
            "tts", ["edge_tts", "soundtools", "pyttsx3"], rng=rng)
        assert chain[0] == head
        counts[head] += 1
    for p, n in counts.items():
        assert 0.28 < n / 3000 < 0.38, f"{p} lead share {n / 3000:.3f} is off-uniform"


def test_weight_roll_keeps_cascade_order(monkeypatch):
    monkeypatch.setenv("AUTOMATO_TTS_WEIGHTS", "edge_tts:2,soundtools:1,pyttsx3:1")
    rng = random.Random(99)
    for _ in range(40):
        chain, head, _cb = ab_test.lead_with(
            "tts", ["edge_tts", "soundtools", "pyttsx3"], rng=rng)
        rest = [p for p in chain if p != head]
        # whatever the roll picked, the rest keep today's safety order
        assert rest == [p for p in ["edge_tts", "soundtools", "pyttsx3"]
                        if p != head]


def test_unweighted_language_leader_never_pushed_behind(monkeypatch):
    # sa chain: vagdhenu (an online primary with NO weight) must stay first even
    # under a heavy weight roll over the local chain.
    monkeypatch.setenv("AUTOMATO_TTS_WEIGHTS", "soundtools:50,edge_tts:1,pyttsx3:1")
    rng = random.Random(7)
    for _ in range(30):
        chain, _head, _cb = ab_test.lead_with(
            "tts", ["vagdhenu", "edge_tts", "soundtools", "pyttsx3"], rng=rng)
        assert chain[0] == "vagdhenu"


def test_no_weights_uses_primary_deterministically(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_WEIGHTS", {})
    monkeypatch.delenv("AUTOMATO_TTS_WEIGHTS", raising=False)
    chain, head, chosen_by = ab_test.lead_with("tts",
                                               ["edge_tts", "soundtools", "pyttsx3"])
    assert chosen_by == "default" and head == "edge_tts"
    assert chain == ["edge_tts", "soundtools", "pyttsx3"]


def test_single_provider_forced_chain_is_default(monkeypatch):
    monkeypatch.setenv("AUTOMATO_TTS_WEIGHTS", "soundtools:10")
    chain, head, chosen_by = ab_test.lead_with("tts", ["soundtools"])
    assert chosen_by == "default" and head == "soundtools"
    assert chain == ["soundtools"]


# ---------------------------------------------------------------------------
# degraded / A/B-exception decision
# ---------------------------------------------------------------------------

def test_health_mode_weight_roll_on_fallback_degrades(monkeypatch):
    monkeypatch.delenv("AUTOMATO_TTS_AB_TEST", raising=False)
    reason, excluded = ab_test.decide_degraded(
        "tts", "en-history-v2", "weight", "soundtools", "soundtools",
        failed_any=False, primary="edge_tts")
    assert reason and not excluded


def test_ab_mode_clean_weight_roll_fallback_does_not_degrade(monkeypatch):
    monkeypatch.setenv("AUTOMATO_TTS_AB_TEST", "1")
    reason, excluded = ab_test.decide_degraded(
        "tts", "en-history-v2", "weight", "soundtools", "soundtools",
        failed_any=False, primary="edge_tts")
    assert reason is None and excluded is True


def test_failure_fallback_degrades_in_both_modes(monkeypatch):
    monkeypatch.setenv("AUTOMATO_TTS_AB_TEST", "1")
    reason, excluded = ab_test.decide_degraded(
        "tts", "en-history-v2", "weight", "edge_tts", "soundtools",
        failed_any=True, primary="edge_tts")
    assert reason and not excluded
    monkeypatch.setenv("AUTOMATO_TTS_AB_TEST", "0")
    reason2, excluded2 = ab_test.decide_degraded(
        "tts", "en-history-v2", "default", "edge_tts", "soundtools",
        failed_any=True, primary="edge_tts")
    assert reason2 and not excluded2


def test_primary_never_degrades(monkeypatch):
    monkeypatch.delenv("AUTOMATO_TTS_AB_TEST", raising=False)
    reason, excluded = ab_test.decide_degraded(
        "tts", "en-history-v2", "weight", "edge_tts", "edge_tts",
        failed_any=False, primary="edge_tts")
    assert reason is None and not excluded


def test_high_tier_forced_to_health_semantics_even_in_ab_mode(monkeypatch):
    monkeypatch.setenv("AUTOMATO_TTS_AB_TEST", "1")
    assert ab_test.ab_enabled_for("tts", "en-finance-v2") is False
    reason, excluded = ab_test.decide_degraded(
        "tts", "en-finance-v2", "weight", "soundtools", "soundtools",
        failed_any=False, primary="edge_tts")
    assert reason and not excluded


def test_low_tier_and_unknown_channel_are_ab_eligible(monkeypatch):
    monkeypatch.setenv("AUTOMATO_TTS_AB_TEST", "1")
    assert ab_test.ab_enabled_for("tts", "en-history-v2") is True
    assert ab_test.ab_enabled_for("tts", "some-future-channel") is True


# ---------------------------------------------------------------------------
# provider_choices.json record
# ---------------------------------------------------------------------------

def test_record_choice_merges_stages(tmp_path):
    ab_test.record_choice(tmp_path, "tts", {"provider": "soundtools",
                                            "chosen_by": "weight"})
    ab_test.record_choice(tmp_path, "assets", {"provider": "pollinations",
                                               "chosen_by": "default"})
    data = json.loads((tmp_path / "provider_choices.json").read_text(encoding="utf-8"))
    assert data["stages"]["tts"]["provider"] == "soundtools"
    assert data["stages"]["assets"]["provider"] == "pollinations"


# ---------------------------------------------------------------------------
# voiceover degraded gate
# ---------------------------------------------------------------------------

def test_gate_degraded_soft_fails_on_degraded_reason():
    g = gate_degraded({"degraded_reason": "provider 'pyttsx3'..."})
    assert g is not None and not g.hard and not g.ok


def test_voiceover_stage_runs_the_degraded_gate():
    outs = {"audio": "x.wav", "word_timings": "y.json",
            "degraded_reason": "provider 'soundtools' served the stage"}
    gates = run_gates("voiceover", outs)
    assert len(gates) == 1 and gates[0].hard is False


# ---------------------------------------------------------------------------
# TTS adapter end-to-end (mocked providers)
# ---------------------------------------------------------------------------

def _ctx(channel="en-history-v2", tts_provider="auto"):
    settings = SimpleNamespace(channel_name=channel, tts_provider=tts_provider)
    return SimpleNamespace(settings=settings)


def _no_voice_catalog(monkeypatch):
    """Kill edge voice resolution without touching the network."""
    monkeypatch.setattr(config, "EDGE_VOICES_CACHE_FILE", Path("missing-voices.json"))
    monkeypatch.setattr("edge_tts.list_voices", RuntimeError("no network"))


def _script(tmp_path):
    p = tmp_path / "script.json"
    p.write_text(json.dumps({"spoken_script": "This is a test narration line "
                                              "for the provider selection test."}),
                 encoding="utf-8")
    return str(p)


def _fake_pyttsx3_writer(run_dir):
    def _pyttsx3(text, run_dir_arg):
        (Path(run_dir_arg) / "voiceover_pyttsx3.wav").write_bytes(b"WAV")
        return str(Path(run_dir_arg) / "voiceover_pyttsx3.wav")
    return _pyttsx3


def test_adapter_clean_weight_roll_fallback_publishes_undegraded(tmp_path, monkeypatch):
    _no_voice_catalog(monkeypatch)
    monkeypatch.setenv("AUTOMATO_TTS_AB_TEST", "1")
    monkeypatch.setenv("AUTOMATO_TTS_WEIGHTS", "edge_tts:0,soundtools:0,pyttsx3:1")
    monkeypatch.setattr(kokoro_tts, "_pyttsx3", _fake_pyttsx3_writer(tmp_path))
    monkeypatch.setattr(kokoro_tts, "_edge_tts",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("must not run")))
    # force the whole assets-style plan: call run directly on the tts adapter
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = kokoro_tts.run(_ctx(), {"script": _script(tmp_path)}, run_dir,
                         session=None)
    assert "degraded_reason" not in out
    rec = json.loads((run_dir / "provider_choices.json").read_text(encoding="utf-8"))
    tts = rec["stages"]["tts"]
    assert tts["provider"] == "pyttsx3"
    assert tts["chosen_by"] == "weight"
    assert tts["excluded_from_degradation"] is True
    assert run_gates("voiceover", out) == []


def test_adapter_failure_fallback_degrades_even_in_ab_mode(tmp_path, monkeypatch):
    _no_voice_catalog(monkeypatch)
    monkeypatch.setenv("AUTOMATO_TTS_AB_TEST", "1")
    # edge leads deterministically (only positive weight), then fails; the
    # cascade runs in today's safety order (soundtools -> pyttsx3).
    monkeypatch.setenv("AUTOMATO_TTS_WEIGHTS", "edge_tts:1,soundtools:0,pyttsx3:0")
    monkeypatch.setattr(kokoro_tts, "_edge_tts",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(kokoro_tts, "_pyttsx3", _fake_pyttsx3_writer(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = kokoro_tts.run(_ctx(), {"script": _script(tmp_path)}, run_dir,
                         session=None)
    assert "degraded_reason" in out
    assert run_gates("voiceover", out)  # soft-fail -> publish downgrades to unlisted
    rec = json.loads((run_dir / "provider_choices.json").read_text(encoding="utf-8"))
    assert rec["stages"]["tts"]["failed"] == ["edge_tts", "soundtools"]


def test_adapter_pinned_filtering_degrades_not_ab_exception(tmp_path, monkeypatch):
    # a PIN is deterministic and bypasses the roll, so landing on a fallback is a
    # deployment choice, not an experiment arm: it must still degrade.
    _no_voice_catalog(monkeypatch)
    monkeypatch.setenv("AUTOMATO_TTS_AB_TEST", "1")
    monkeypatch.setenv("AUTOMATO_TTS_PIN", "pyttsx3")
    monkeypatch.setattr(kokoro_tts, "_pyttsx3", _fake_pyttsx3_writer(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = kokoro_tts.run(_ctx(), {"script": _script(tmp_path)}, run_dir,
                         session=None)
    assert "degraded_reason" in out
    rec = json.loads((run_dir / "provider_choices.json").read_text(encoding="utf-8"))
    assert rec["stages"]["tts"]["chosen_by"] == "pinned"
    assert rec["stages"]["tts"]["excluded_from_degradation"] is False


# ---------------------------------------------------------------------------
# assets stage: weighted lead + degraded/rescue semantics
# ---------------------------------------------------------------------------

def _assets_inputs(tmp_path, n=4):
    script = {"image_prompts": [f"prompt {i}" for i in range(n)]}
    p = tmp_path / "script.json"
    p.write_text(json.dumps(script), encoding="utf-8")
    return {"script": str(p), "image_count": n}


def _fake_top_up_fallback(tmp_path, n=4):
    def _top_up(assets_dir, saved, prompts, remaining):
        made = 0
        for i in range(remaining):
            fname = Path(assets_dir) / f"bg_{len(saved):02d}.jpg"
            fname.write_bytes(b"DISTINCT-" + str(len(saved)).encode())
            saved.append(str(fname))
            made += 1
        return made
    return _top_up


def test_assets_ab_clean_weight_roll_undegraded(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMATO_ASSETS_AB_TEST", "1")
    monkeypatch.setenv("AUTOMATO_ASSETS_WEIGHTS", "perchance:0,pollinations:1")
    monkeypatch.setattr(config, "PERCHANCE_ENABLED", True)
    monkeypatch.setattr(perchance_images, "_top_up_fallback",
                        _fake_top_up_fallback(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = perchance_images.run(_ctx(), _assets_inputs(tmp_path), run_dir,
                               session=None)
    assert len(out["image_files"]) == 4
    assert "degraded_reason" not in out
    rec = json.loads((run_dir / "provider_choices.json").read_text(encoding="utf-8"))
    assets = rec["stages"]["assets"]
    assert assets["provider"] == "pollinations"
    assert assets["chosen_by"] == "weight"
    assert assets["excluded_from_degradation"] is True
    assert run_gates("assets", {"image_files": out["image_files"]},
                     requested_images=4) == []


def test_assets_health_mode_weight_roll_degrades(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOMATO_ASSETS_AB_TEST", raising=False)
    monkeypatch.setenv("AUTOMATO_ASSETS_WEIGHTS", "perchance:0,pollinations:1")
    monkeypatch.setattr(config, "PERCHANCE_ENABLED", True)
    monkeypatch.setattr(perchance_images, "_top_up_fallback",
                        _fake_top_up_fallback(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = perchance_images.run(_ctx(), _assets_inputs(tmp_path), run_dir,
                               session=None)
    assert "degraded_reason" in out
    assert run_gates("assets", out, requested_images=4)  # soft-fail on fallback


def test_assets_rescue_after_shortfall_degrades_in_both_modes(monkeypatch):
    for ab in ("1", "0"):
        monkeypatch.setenv("AUTOMATO_ASSETS_AB_TEST", ab)
        reason, excluded = ab_test.decide_degraded(
            "assets", "en-history-v2", "default", "perchance", "pollinations",
            failed_any=True, primary="perchance")
        assert reason and not excluded


def test_assets_high_tier_weight_roll_still_degrades(monkeypatch):
    monkeypatch.setenv("AUTOMATO_ASSETS_AB_TEST", "1")
    reason, excluded = ab_test.decide_degraded(
        "assets", "en-finance-v2", "weight", "pollinations", "pollinations",
        failed_any=False, primary="perchance")
    assert reason and not excluded


# ---------------------------------------------------------------------------
# scripting stage: weighted lead / explicit-provider pin
# ---------------------------------------------------------------------------

def test_scripting_explicit_provider_is_an_operator_pin(monkeypatch):
    # a heavy roll in the env could never beat an explicit operator provider
    monkeypatch.setenv("AUTOMATO_LLM_WEIGHTS", "ai_studio:1,duckai:100")
    providers, picked, chosen_by, base = generic_llm._lead_providers("duckai")
    assert chosen_by == "pinned" and picked == "duckai"
    assert providers[0] == "duckai"
    expected = ["duckai"] + [p for p in config.LLM_NO_LOGIN_CHAIN if p
                             and p != "duckai"]
    assert providers == expected


def test_scripting_default_enters_weight_roll(monkeypatch):
    all_chain = ["duckai", "ai_studio", *config.LLM_NO_LOGIN_CHAIN]
    weights = {p: (1 if p == "duckai" else 0) for p in all_chain}
    monkeypatch.setenv("AUTOMATO_LLM_WEIGHTS",
                       ",".join(f"{p}:{w}" for p, w in weights.items()))
    providers, picked, chosen_by, _base = generic_llm._lead_providers("ai_studio")
    assert chosen_by == "weight" and picked == "duckai"
    assert providers[0] == "duckai"
    assert "ai_studio" in providers  # still in the cascade as a fallback

# ---------------------------------------------------------------------------
# analytics parsing + correlation rollup
# ---------------------------------------------------------------------------

def test_parse_metrics_highlight_cards():
    body = ("Reach\nViews\n1,234\nAVG. VIEW DURATION\n0:45\n"
            "Impressions\n5,678")
    parsed = studio_metrics.parse_metrics(body)
    assert parsed["views"] == 1234
    assert parsed["avg_view_duration_s"] == 45.0


def test_parse_metrics_inline_views_and_missing_duration():
    body = "1.2K views\nsomething else"
    parsed = studio_metrics.parse_metrics(body)
    assert parsed["views"] == 1200
    assert parsed["avg_view_duration_s"] is None


def test_parse_metrics_no_data():
    body = "No data available for this video yet"
    assert studio_metrics.parse_metrics(body)["views"] is None


def test_duration_parsing():
    assert studio_metrics._parse_duration("1:02:30") == 3750.0
    assert studio_metrics._parse_duration("0:07") == 7.0
    assert studio_metrics._parse_duration("abc") is None


def _fake_run(tmp_path, run_id, url, choices=None, perf=None):
    d = tmp_path / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run_state.json").write_text(
        json.dumps({"run_id": run_id, "status": "done"}), encoding="utf-8")
    if url:
        (d / "post_url.json").write_text(
            json.dumps({"status": "uploaded", "visibility": "unlisted",
                        "url": url}), encoding="utf-8")
    if choices:
        (d / "provider_choices.json").write_text(
            json.dumps({"stages": choices}), encoding="utf-8")
    if perf is not None:
        (d / "performance.json").write_text(json.dumps(perf), encoding="utf-8")
    return d


def test_correlation_rollup_handles_no_data_gracefully(tmp_path):
    _fake_run(tmp_path, "run_a",
              "https://www.youtube.com/watch?v=AAAAA111111",
              choices={"tts": {"provider": "edge_tts", "chosen_by": "weight"}},
              perf={"views": 1000, "avg_view_duration_s": 30.0, "status": "ok"})
    _fake_run(tmp_path, "run_b",
              "https://www.youtube.com/watch?v=BBBBB222222",
              choices={"tts": {"provider": "soundtools", "chosen_by": "weight",
                               "degraded": False}},
              perf={"status": "no_data"})   # too recent: legitimately no analytics
    _fake_run(tmp_path, "run_c",
              "https://www.youtube.com/watch?v=CCCCC333333",
              choices={"tts": {"provider": "edge_tts", "chosen_by": "default"}})
    rows = ab_results.rollup(list(ab_results.entries(root=tmp_path,
                                                     min_age_days=0.0)))
    assert rows, "rollup must not error on missing/no-data analytics"
    by_provider = {r["provider"]: r for r in rows if r["stage"] == "tts"}
    assert by_provider["edge_tts"]["n"] == 2
    assert by_provider["edge_tts"]["with_analytics"] == 1
    assert by_provider["edge_tts"]["awaiting_data"] == 1
    assert by_provider["soundtools"]["n"] == 1
    assert by_provider["soundtools"]["awaiting_data"] == 1
    assert by_provider["soundtools"]["mean_views"] is None


def test_fetch_performance_skips_without_session(tmp_path):
    run_dir = _fake_run(tmp_path, "run_x",
                        "https://www.youtube.com/watch?v=XXXXX999999",
                        choices={"tts": {"provider": "edge_tts"}})
    out = ab_results.fetch_performance(root=tmp_path, session=None, min_age_days=0.0)
    assert out == [{"run_id": "run_x", "status": "skipped",
                    "reason": "no logged-in session"}]
    assert not (run_dir / "performance.json").is_file()


def test_fetch_performance_respects_min_age(tmp_path):
    _fake_run(tmp_path, "run_y",
              "https://www.youtube.com/watch?v=YYYYY000000",
              choices={"tts": {"provider": "edge_tts"}})
    out = ab_results.fetch_performance(root=tmp_path, session=None,
                                       min_age_days=7.0)
    # the fake run was just created (age ~0), so it must be skipped silently
    assert out == []


def test_fetch_performance_skips_run_with_real_analytics(tmp_path):
    """A run with real analytics is final — never re-scraped."""
    _fake_run(tmp_path, "run_ok",
              "https://www.youtube.com/watch?v=OKXXX000002",
              choices={"tts": {"provider": "edge_tts"}},
              perf={"views": 42, "avg_view_duration_s": 5.0, "status": "ok"})
    out = ab_results.fetch_performance(root=tmp_path, session=None,
                                       min_age_days=0.0)
    assert out == []


def test_fetch_performance_retries_no_data_records(tmp_path, monkeypatch):
    """A persisted no_data record is a transient miss, not a lock: a later
    fetch must re-attempt it once real analytics exist, else one slow Studio
    render could silently starve the A/B correlation forever."""
    run_dir = _fake_run(
        tmp_path, "run_nd",
        "https://www.youtube.com/watch?v=NDXXX000001",
        choices={"tts": {"provider": "edge_tts"}},
        perf={"status": "no_data"})

    class _Session:
        def first_page(self):
            return None

    monkeypatch.setattr(
        ab_results.studio_metrics, "fetch_metrics",
        lambda page, video_id: {"views": 77, "avg_view_duration_s": 9.0,
                                "video_id": video_id, "status": "ok"})
    fetched = ab_results.fetch_performance(root=tmp_path, session=_Session(),
                                           min_age_days=0.0)
    assert fetched == [{"run_id": "run_nd", "status": "ok", "views": 77}]
    recorded = json.loads((run_dir / "performance.json").read_text(encoding="utf-8"))
    assert recorded["views"] == 77
    assert recorded["status"] == "ok"


def test_legacy_choices_backfills_regression(tmp_path):
    d = _fake_run(tmp_path, "run_z", url=None)
    (d / "word_timings.json").write_text(
        json.dumps({"provider": "edge_tts", "voice": "en-US-ChristopherNeural"}),
        encoding="utf-8")
    choices = ab_results.all_choices(d)
    assert choices["tts"]["provider"] == "edge_tts"
    assert choices["assets"]["provider"] == "perchance"
