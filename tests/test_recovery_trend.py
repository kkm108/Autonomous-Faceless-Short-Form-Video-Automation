"""R3-W1: recovery-trend ledger + drift early-warning heuristic."""
import json
from datetime import date, timedelta

from automato import config
from automato.resilience import recovery


def _write_trend(tmp_path, data):
    p = tmp_path / "recovery_trend.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_track_and_aggregate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    recovery._track_recovery("youtube", True)
    recovery._track_recovery("youtube", False)
    recovery._track_recovery("perchance", True)

    trend = recovery.recovery_trend()
    assert trend["youtube"]["attempts"] == 2
    assert trend["youtube"]["succeeded"] == 1
    assert trend["youtube"]["success_rate"] == 0.5
    assert trend["perchance"]["attempts"] == 1
    assert trend["__totals__"]["attempts"] == 3
    assert trend["__totals__"]["succeeded"] == 2


def test_trend_filtered_by_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    recovery._track_recovery("youtube", True)
    recovery._track_recovery("perchance", True)
    trend = recovery.recovery_trend(provider="youtube")
    assert "perchance" not in trend
    assert trend["youtube"]["attempts"] == 1


def test_alert_fires_when_recovery_rate_climbs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    today = date.today()

    def day(delta):
        return (today - timedelta(days=delta)).isoformat()

    data = {}
    for i in range(8, 15):                       # baseline: 1 attempt/day
        data[day(i)] = {"youtube": {"attempts": 1, "succeeded": 1}}
    for i in range(0, 7):                        # recent: 6 attempts/day
        data[day(i)] = {"youtube": {"attempts": 6, "succeeded": 5}}

    # subtle: the same day keys must not collide; 0..7 and 8..14 are disjoint.
    _write_trend(tmp_path, data)
    alerts = recovery.trend_alert()
    assert [a["provider"] for a in alerts] == ["youtube"]
    assert alerts[0]["recent_attempts"] == 42
    assert alerts[0]["baseline_attempts"] == 7


def test_no_alert_when_flat(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    today = date.today()

    def day(delta):
        return (today - timedelta(days=delta)).isoformat()

    data = {}
    for i in range(0, 14):
        data[day(i)] = {"youtube": {"attempts": 2, "succeeded": 2}}
    _write_trend(tmp_path, data)
    assert recovery.trend_alert() == []
