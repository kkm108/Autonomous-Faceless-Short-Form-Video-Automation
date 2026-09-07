"""Whole-run process guard for scheduled/unattended use (R2-W3 / R2-F3).

The existing per-provider ``session.lock`` is deliberately *advisory* — it warns,
never blocks, so a stale lock after a crash never wedges the engine. That is the
right behavior *within* a run, but nothing stops a scheduler from starting a
second run while the first is still holding the same Chromium profiles — the
exact scenario that corrupts profiles or trips Chromium's own instance lock.

This module adds a real, **staleness-aware** whole-run lock:

  * A fresh lock (heartbeat younger than ``config.RUN_LOCK_STALE_S``) means
    another run is actively working on this engine root — the second run is
    refused with a hard error instead of racing it.
  * A stale lock (no heartbeat for longer than the threshold) means the run that
    left it almost certainly crashed — the lock is reclaimed with a warning, so
    a dead process can never permanently wedge scheduling (same ethos as the
    advisory session lock, but at run granularity).

Heartbeat: the orchestrator refreshes the lock at every stage boundary, and
``heartbeat()`` is cheap so callers may refresh it more often inside long waits.
A reasonable standing policy is: minimum scheduling interval >= 2x the longest
observed run duration, and the stale threshold is a floor for how long a crashed
run can hold the engine (default 1 hour).
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional

from . import config

log = logging.getLogger(__name__)

LOCK_FILENAME = "run.lock"


class RunGuardError(Exception):
    """A second run is already actively using this engine root."""


def _lock_file(run_root: Path) -> Path:
    return Path(run_root) if Path(run_root).suffix == ".lock" else \
        Path(run_root) / LOCK_FILENAME


def _read_lock(path: Path) -> Optional[Dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _write_lock(path: Path, pid: int, started_at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "started_at": started_at,
                    "heartbeat": time.time()}, indent=2),
        encoding="utf-8",
    )


def _is_stale(entry: Optional[Dict], stale_s: Optional[float] = None) -> bool:
    if entry is None:
        return True
    threshold = stale_s if stale_s is not None else config.RUN_LOCK_STALE_S
    age = time.time() - float(entry.get("heartbeat", entry.get("started_at", 0.0)))
    return age >= threshold


def _pid_my_own(entry: Optional[Dict]) -> bool:
    return bool(entry) and int(entry.get("pid", -1)) == os.getpid()


def acquire_run_lock(run_root: Path, stale_s: Optional[float] = None) -> object:
    """Claim the engine-root run lock, refusing to race an active run.

    Returns a small handle with ``heartbeat()``; the caller MUST ``release()``
    (via :func:`run_lock` context manager) so the lock file is removed.
    """
    path = _lock_file(run_root)
    entry = _read_lock(path)

    if entry is not None and not _is_stale(entry, stale_s) and not _pid_my_own(entry):
        age = time.time() - float(entry.get("heartbeat", 0))
        raise RunGuardError(
            f"Another run appears to be active on this engine root "
            f"(run.lock heartbeat {int(age)}s old). Refusing to start a "
            f"concurrent run — it would share the same Chromium profiles. "
            f"If the other run actually died, wait for the lock to go stale "
            f"({stale_s or config.RUN_LOCK_STALE_S}s) or delete "
            f"{path}."
        )
    if entry is not None and not _pid_my_own(entry):
        log.warning("Reclaiming stale run.lock (%ds old); previous run likely died",
                    int(time.time() - float(entry.get("heartbeat", 0))))

    pid = os.getpid()
    started_at = time.time()
    _write_lock(path, pid, started_at)

    class _Handle:
        def heartbeat(self) -> None:
            _write_lock(path, pid, started_at)

        def release(self) -> None:
            try:
                if _read_lock(path) and _read_lock(path).get("pid") == pid:
                    path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                log.warning("Could not remove run.lock %s", path)

    return _Handle()


@contextmanager
def run_lock(run_root: Path, stale_s: Optional[float] = None) -> Iterator[object]:
    """Context-manager form of :func:`acquire_run_lock`."""
    handle = acquire_run_lock(run_root, stale_s)
    try:
        yield handle
    finally:
        handle.release()
