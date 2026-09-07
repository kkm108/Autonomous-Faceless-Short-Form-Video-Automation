"""R2-W3 / R2-F3: staleness-aware whole-run process guard tests."""
import json
import os
import time

import pytest

from automato import config
from automato.run_guard import (
    RunGuardError,
    _is_stale,
    _lock_file,
    acquire_run_lock,
    run_lock,
)


def _stale_entry(age_s: float) -> dict:
    return {"pid": 9999, "started_at": time.time() - age_s,
            "heartbeat": time.time() - age_s}


def test_lock_file_path(tmp_path):
    assert _lock_file(tmp_path) == tmp_path / "run.lock"
    assert _lock_file(tmp_path / "x.lock") == tmp_path / "x.lock"


def test_is_stale_detects_fresh_vs_stale():
    fresh = _is_stale(_stale_entry(10), stale_s=3600)
    stale = _is_stale(_stale_entry(7200), stale_s=3600)
    assert fresh is False and stale is True
    assert _is_stale(None) is True


def test_acquire_refuses_active_foreign_run(tmp_path):
    (tmp_path / "run.lock").write_text(
        json.dumps(_stale_entry(10)), encoding="utf-8")
    with pytest.raises(RunGuardError):
        acquire_run_lock(tmp_path, stale_s=3600)


def test_acquire_reclaims_stale_lock(tmp_path):
    (tmp_path / "run.lock").write_text(
        json.dumps(_stale_entry(7200)), encoding="utf-8")
    h = acquire_run_lock(tmp_path, stale_s=3600)
    entry = json.loads((tmp_path / "run.lock").read_text(encoding="utf-8"))
    assert entry["pid"] == os.getpid()
    assert "heartbeat" in entry
    h.release()
    assert not (tmp_path / "run.lock").exists()


def test_context_manager_releases_on_body(tmp_path):
    with run_lock(tmp_path, stale_s=60) as h:
        h.heartbeat()
        assert (tmp_path / "run.lock").exists()
    assert not (tmp_path / "run.lock").exists()


def test_release_preserves_foreign_lock(tmp_path):
    # Our own release must not delete a lock another process wrote meanwhile.
    h = acquire_run_lock(tmp_path, stale_s=60)
    (tmp_path / "run.lock").write_text(
        json.dumps(_stale_entry(5)), encoding="utf-8")
    h.release()
    assert (tmp_path / "run.lock").exists()


def test_same_process_can_reacquire(tmp_path):
    h1 = acquire_run_lock(tmp_path)
    h2 = acquire_run_lock(tmp_path)  # same pid -> allowed
    h1.release()
    h2.release()
    assert not (tmp_path / "run.lock").exists()


def test_default_stale_threshold_from_config():
    assert config.RUN_LOCK_STALE_S > 0
    assert _is_stale(_stale_entry(config.RUN_LOCK_STALE_S), stale_s=None) is True
    assert _is_stale(_stale_entry(config.RUN_LOCK_STALE_S / 2), stale_s=None) is False


def test_run_workflow_heartbeats_lock_handle_and_releases(tmp_path, monkeypatch):
    """Regression (R4): run_workflow must heartbeat the lock *handle* run_lock
    yields, not the generator context-manager wrapper (which lacks heartbeat),
    and must release the lock when done. The old code crashed the CLI's very
    first run with `_GeneratorContextManager has no attribute 'heartbeat'`."""
    import automato.orchestrator as orch

    monkeypatch.setattr(orch.config, "OUTPUT_DIR", tmp_path)
    marker = {}

    def fake_locked(workflow_name, seed, resume_run_id, settings, guard):
        guard.heartbeat()  # the exact call that used to crash
        marker["locked"] = True
        return marker

    monkeypatch.setattr(orch, "_run_workflow_locked", fake_locked)
    out = orch.run_workflow("faceless_short", {"topic": "t"}, settings=None)
    assert out is marker
    assert marker["locked"] is True
    assert not (tmp_path / "run.lock").exists()  # released on exit
