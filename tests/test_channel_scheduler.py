"""Channel-scheduling layer tests.

The scheduler is pure (reads run history, computes weights, records a plan —
never executes runs). These tests pin:
  * upload-history scanning across the pre/post-R7 record formats,
  * the staleness-weighted formula (+ high-tier floor, + jitter),
  * day-plan selection (budget, no replacement, determinism via seed),
  * the audit trail record.
"""
import json
import random
from datetime import date, datetime

import pytest

from automato import channel_scheduler as sched


def _epoch(midnight_day: "date") -> float:
    return datetime(*midnight_day.timetuple()[:3]).timestamp()


def _mock_whitelist(monkeypatch, names: "dict[str, str]") -> None:
    monkeypatch.setenv("AUTOMATO_CHANNEL_WHITELIST", json.dumps(names))


def _make_run(root, run_id, *, attempted_at=None, channel=None,
              post_url=None, topic=None):
    d = root / run_id
    d.mkdir(parents=True)
    if channel is not None and attempted_at is not None:
        (d / "upload_attempted.json").write_text(
            json.dumps({"attempted_at": attempted_at, "channel": channel,
                        "title": "x", "visibility": "unlisted"}),
            encoding="utf-8")
    if post_url is not None:
        (d / "post_url.json").write_text(
            json.dumps(post_url), encoding="utf-8")
    if topic is not None:
        (d / "run_state.json").write_text(
            json.dumps({"run_id": run_id, "seed": {"topic": topic},
                        "status": "done", "created_at": attempted_at}),
            encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# upload_history
# ---------------------------------------------------------------------------

def test_upload_history_picks_newest_per_channel(tmp_path, monkeypatch):
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    _make_run(tmp_path, "1000_aaaa", channel="main",
              attempted_at=_epoch(date(2026, 9, 1)))
    _make_run(tmp_path, "1001_bbbb", channel="main",
              attempted_at=_epoch(date(2026, 9, 5)))
    _make_run(tmp_path, "1002_cccc", channel="es-finance",
              attempted_at=_epoch(date(2026, 9, 9)))

    hist = sched.upload_history(tmp_path)
    by = {h.channel: h for h in hist}
    assert set(by) == {"main", "es-finance"}
    assert by["main"].last_upload_run_id == "1001_bbbb"
    assert by["es-finance"].last_upload_epoch == _epoch(date(2026, 9, 9))


def test_upload_history_prefers_attempted_over_posturl(tmp_path, monkeypatch):
    """upload_attempted.json (channel + exact time) wins over post_url.json's
    run-id-estimated time when both are present."""
    _mock_whitelist(monkeypatch, {"main": "A" * 24})
    # post_url epoch (run id prefix) is much earlier than attempted_at
    _make_run(tmp_path, "1000_aaaa",
              channel="main", attempted_at=_epoch(date(2026, 9, 9)),
              post_url={"status": "uploaded", "channel": "main",
                        "url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"})
    hist = sched.upload_history(tmp_path)
    assert hist[0].channel == "main"
    assert hist[0].last_upload_epoch == pytest.approx(_epoch(date(2026, 9, 9)), abs=1)


def test_upload_history_posturl_channel_fallback(tmp_path, monkeypatch):
    """Legacy runs that only wrote post_url.json with a channel field."""
    _mock_whitelist(monkeypatch, {"main": "A" * 24})
    _make_run(tmp_path, "1000_aaaa",
              post_url={"status": "uploaded", "channel": "main",
                        "url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"})
    hist = sched.upload_history(tmp_path)  # now=time.time, epoch from run id
    assert len(hist) == 1
    assert hist[0].channel == "main"
    assert hist[0].last_upload_run_id == "1000_aaaa"


def test_upload_history_run_state_topic_fallback(tmp_path, monkeypatch):
    """Oldest runs: channel only derivable from the seed topic's keyword map."""
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    _make_run(tmp_path, "1000_aaaa",
              attempted_at=_epoch(date(2026, 9, 3)),
              topic="coding puzzle for a night")
    hist = sched.upload_history(tmp_path)
    assert len(hist) == 1
    assert hist[0].channel == "main"  # 'coding' routes to main


def test_upload_history_ignores_junk_dirs(tmp_path, monkeypatch):
    _mock_whitelist(monkeypatch, {"main": "A" * 24})
    (tmp_path / "not_a_run").mkdir()
    (tmp_path / "_verify_caption_fix").mkdir()
    assert sched.upload_history(tmp_path) == []


# ---------------------------------------------------------------------------
# compute_weights
# ---------------------------------------------------------------------------

def test_never_uploaded_uses_never_staleness(tmp_path, monkeypatch):
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    monkeypatch.setenv("AUTOMATO_SCHEDULER_NEVER_UPLOADED_STALENESS", "30")
    weights = {w.channel: w for w in sched.compute_weights([], rng=random.Random(0))}
    assert set(weights) == {"main", "es-finance"}
    assert weights["main"].days_since_last == 30.0
    assert weights["main"].staleness_weight == 30.0
    assert weights["main"].tier_floor == 0.0
    # high-tier floor guarantees finance never sinks below the floor on day 0
    assert weights["es-finance"].tier_floor == 2.0
    assert weights["es-finance"].effective_weight > weights["main"].effective_weight


def test_high_tier_floor_applies_always(tmp_path, monkeypatch):
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    h = [
        sched.ChannelHistory("main", _epoch(date(2026, 9, 10)), "r1", 0.0),
        sched.ChannelHistory("es-finance", _epoch(date(2026, 9, 10)), "r2", 0.0),
    ]
    weights = {w.channel: w for w in sched.compute_weights(h, rng=random.Random(1))}
    # Even when both are fresh, the high-tier floor lifts finance above main.
    assert weights["es-finance"].tier_floor == 2.0
    assert weights["es-finance"].effective_weight > weights["main"].effective_weight
    assert weights["main"].tier_floor == 0.0
    assert 0 <= weights["main"].random_component <= 0.5


def test_staleness_can_beat_tier_floor(tmp_path, monkeypatch):
    """A very stale low-tier channel (25d) outweights a fresh high-tier one
    (2d + floor): the floor slows the slide, it does not override staleness."""
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    h = [
        sched.ChannelHistory("main", _epoch(date(2026, 8, 20)), "r1", 25.0),
        sched.ChannelHistory("es-finance", _epoch(date(2026, 9, 15)), "r2", 2.0),
    ]
    weights = {w.channel: w for w in sched.compute_weights(h, rng=random.Random(2))}
    assert weights["main"].effective_weight > weights["es-finance"].effective_weight


# ---------------------------------------------------------------------------
# select_channels_for_day
# ---------------------------------------------------------------------------

def test_selection_respects_budget_and_no_replacement(tmp_path, monkeypatch):
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    monkeypatch.setenv("AUTOMATO_SCHEDULER_BUDGET", "7")
    plan = sched.select_channels_for_day(
        target_date=date(2026, 9, 20), root=tmp_path, seed=7)
    assert plan.budget == 7
    assert set(plan.selected) == {"main", "es-finance"}  # capped at channel count
    assert len(plan.selected) == len(set(plan.selected))  # no duplicates


def test_selection_staleness_dominates_frequency(tmp_path, monkeypatch):
    """main 10 days stale vs es-finance 1 day old (+2 high-tier floor):

       P(main) ~= 10 / (10 + 1 + 2) ~= 0.77,

    so across many seeds the stale channel is picked most of the time BUT the
    fresh high-tier channel still wins occasionally — the small random component
    keeps the order imperfectly predictable without abandoning staleness."""
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    _make_run(tmp_path, "1000_aaaa", channel="main",
              attempted_at=_epoch(date(2026, 9, 10)))
    _make_run(tmp_path, "1001_bbbb", channel="es-finance",
              attempted_at=_epoch(date(2026, 9, 19)))
    picks = {"main": 0, "es-finance": 0}
    for seed in range(200):
        plan = sched.select_channels_for_day(
            target_date=date(2026, 9, 20), budget=1, root=tmp_path, seed=seed)
        assert len(plan.selected) == 1
        picks[plan.selected[0]] += 1
    # dominant-staleness wins ~77%: sanity band far from equality or certainty
    assert picks["main"] > picks["es-finance"]
    assert picks["main"] > 120
    # and the random component is real (both outcomes occurred)
    assert 0 < picks["es-finance"] < 200


def test_selection_is_seed_deterministic(tmp_path, monkeypatch):
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    a = sched.select_channels_for_day(
        target_date=date(2026, 9, 20), budget=1, root=tmp_path, seed=99)
    b = sched.select_channels_for_day(
        target_date=date(2026, 9, 20), budget=1, root=tmp_path, seed=99)
    assert a.selected == b.selected
    assert a.weights == b.weights


def test_env_budget_override(tmp_path, monkeypatch):
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    monkeypatch.setenv("AUTOMATO_SCHEDULER_BUDGET", "1")
    plan = sched.select_channels_for_day(
        target_date=date(2026, 9, 20), root=tmp_path, seed=3)
    assert plan.budget == 1
    assert len(plan.selected) == 1


def test_channels_active_on_date(tmp_path, monkeypatch):
    """Only channels with an upload attempt ON the target day count as active
    (crash-recovery re-run must not re-pick them). A channel is judged by its
    NEWEST upload (upload_history keeps the latest per channel)."""
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    _make_run(tmp_path, "1000_aaaa", channel="main",
              attempted_at=_epoch(date(2026, 9, 15)))
    _make_run(tmp_path, "1001_bbbb", channel="main",
              attempted_at=_epoch(date(2026, 9, 17)))   # main's newest = 09-17
    _make_run(tmp_path, "1002_cccc", channel="es-finance",
              attempted_at=_epoch(date(2026, 9, 16)))
    assert sched.channels_active_on_date(tmp_path, date(2026, 9, 17)) == {"main"}
    assert sched.channels_active_on_date(tmp_path, date(2026, 9, 16)) == {"es-finance"}
    # main's 09-15 upload is shadowed by its newer 09-17 one
    assert sched.channels_active_on_date(tmp_path, date(2026, 9, 15)) == set()
    assert sched.channels_active_on_date(tmp_path, date(2026, 9, 10)) == set()


def test_selection_excludes_today_active(tmp_path, monkeypatch):
    """A channel that already uploaded today is excluded from the pool but
    still listed in the weight table (auditable)."""
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    plan = sched.select_channels_for_day(
        target_date=date(2026, 9, 20), budget=1, root=tmp_path, seed=4,
        exclude={"main"})
    assert plan.selected == ["es-finance"]
    assert plan.excluded == ["main"]
    # the table still shows both channels (main marked excluded)
    assert {w.channel for w in plan.weights} == {"main", "es-finance"}


def test_selection_excludes_and_budget_topup(tmp_path, monkeypatch):
    """With 2 channels, budget 2 and 1 excluded: only the non-excluded channel
    can take slots (no phantom re-pick of the excluded one)."""
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    plan = sched.select_channels_for_day(
        target_date=date(2026, 9, 20), budget=2, root=tmp_path, seed=4,
        exclude={"main"})
    assert plan.selected == ["es-finance"]
    assert "main" not in plan.selected
    assert len(plan.selected) == 1  # only one candidate remains


def test_target_date_controls_staleness(tmp_path, monkeypatch):
    """Staleness is measured at the target day's midnight, so a plan for a
    future date sees a 10-day-old upload as 10.0 days."""
    _mock_whitelist(monkeypatch, {"main": "A" * 24})
    _make_run(tmp_path, "1000_aaaa", channel="main",
              attempted_at=_epoch(date(2026, 9, 10)))
    plan = sched.select_channels_for_day(
        target_date=date(2026, 9, 20), root=tmp_path, seed=0)
    w = next(x for x in plan.weights if x.channel == "main")
    assert w.days_since_last == pytest.approx(10.0, abs=0.1)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_record_day_plan_appends_exactly_one_entry(tmp_path, monkeypatch):
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    plan = sched.select_channels_for_day(
        target_date=date(2026, 9, 20), budget=2, root=tmp_path, seed=11)
    path = sched.record_day_plan(plan, root=tmp_path)
    assert path == tmp_path / "channel_selections.json"

    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 1
    entry = data[0]
    assert entry["date"] == "2026-09-20"
    assert entry["budget"] == 2
    assert entry["selected"] == plan.selected
    # the fairness claim is auditable: weights that produced the choice
    assert {w["channel"] for w in entry["weights"]} == {"main", "es-finance"}
    for w in entry["weights"]:
        assert w["staleness_weight"] >= 0.0
        assert w["tier_floor"] >= 0.0
        assert w["effective_weight"] == pytest.approx(
            w["staleness_weight"] + w["tier_floor"] + w["random_component"], abs=0.01)
    assert entry["recorded_at"] > 0


def test_record_day_plan_same_date_is_idempotent(tmp_path, monkeypatch):
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    plan = sched.select_channels_for_day(
        target_date=date(2026, 9, 20), budget=1, root=tmp_path, seed=5)
    sched.record_day_plan(plan, root=tmp_path)
    sched.record_day_plan(plan, root=tmp_path)
    data = json.loads((tmp_path / "channel_selections.json").read_text(encoding="utf-8"))
    assert len(data) == 1  # re-run of the same day replaces, not duplicates


def test_record_day_plan_distinct_dates_accumulate(tmp_path, monkeypatch):
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    for d in (date(2026, 9, 20), date(2026, 9, 21)):
        plan = sched.select_channels_for_day(
            target_date=d, budget=1, root=tmp_path, seed=5)
        sched.record_day_plan(plan, root=tmp_path)
    data = json.loads((tmp_path / "channel_selections.json").read_text(encoding="utf-8"))
    assert len(data) == 2
    assert [e["date"] for e in data] == ["2026-09-20", "2026-09-21"]


def test_record_day_plan_with_run_outcomes(tmp_path, monkeypatch):
    """scheduled-run replaces the allocation entry with one carrying outcomes:
    run id + status (+ url when published), so the audit shows delivery, not
    just allocation."""
    _mock_whitelist(monkeypatch, {"main": "A" * 24, "es-finance": "B" * 24})
    plan = sched.select_channels_for_day(
        target_date=date(2026, 9, 20), budget=1, root=tmp_path, seed=6)
    runs = {"es-finance": {
        "run_id": "1789000000_abc123", "status": "done",
        "url": "https://www.youtube.com/watch?v=ILOVEESFINANCE", "visibility": "public",
    }}
    sched.record_day_plan(plan, root=tmp_path)  # allocation first
    sched.record_day_plan(plan, root=tmp_path, runs=runs)  # outcomes replace it
    data = json.loads((tmp_path / "channel_selections.json").read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["runs"] == runs
    assert data[0]["selected"] == plan.selected
