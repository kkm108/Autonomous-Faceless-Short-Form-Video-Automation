"""R3-W6: RunState.load() must not let a user-supplied --resume id shape a
filesystem pattern unsafely."""
import pytest

from automato import config
from automato.state import RunState


def test_load_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="Invalid run id"):
        RunState.load("..")
    with pytest.raises(FileNotFoundError, match="Invalid run id"):
        RunState.load("a/b")


def test_load_rejects_glob_metachars(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="Invalid run id"):
        RunState.load("1700000000_abcde[0]")
    with pytest.raises(FileNotFoundError, match="Invalid run id"):
        RunState.load("1700000000_*")


def test_load_valid_id_gets_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        RunState.load("1700000000_abc123")
    assert "No run state found" in str(excinfo.value)


def test_run_state_records_wall_clock_and_stage_timings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    state = RunState.create("faceless_short", {"topic": "history"})
    assert state.created_at is not None

    state.mark_completed("assets", {"images": "x"})
    state.mark_stage_timing("assets", 12.345)

    import json
    payload = json.loads(
        (tmp_path / state.run_id / "run_state.json").read_text(encoding="utf-8"))
    assert payload["created_at"] == state.created_at
    assert payload["timings"] == {"assets": 12.345}
    assert payload["total_s"] is None  # not done yet

    state.mark_done()
    payload = json.loads(
        (tmp_path / state.run_id / "run_state.json").read_text(encoding="utf-8"))
    assert payload["status"] == "done"
    assert payload["completed_at"] is not None
    assert payload["total_s"] == round(payload["completed_at"] - payload["created_at"], 3)

    restored = RunState.load(state.run_id)
    assert restored.created_at == state.created_at
    assert restored.completed_at == state.completed_at
    assert restored.timings == {"assets": 12.345}
    assert restored.total_s == payload["total_s"]
