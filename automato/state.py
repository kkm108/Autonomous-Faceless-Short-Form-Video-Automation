"""Run-state persistence and resume.

State is a small JSON ledger under output/state.json keyed by run id. It records
which stages completed and where each artifact lives, so a crashed run can resume
at the first incomplete stage rather than starting over or requiring a human to
bridge data.
"""
from __future__ import annotations

import glob
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from . import config

log = logging.getLogger(__name__)

# Run ids are generated internally as "<epoch>_<hex>"; the resume CLI takes one
# from the user, so we validate the charset and glob-escape it before it shapes a
# filesystem pattern (R3-W6).
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class RunState:
    def __init__(self, run_id: str, workflow: str, seed: Dict[str, Any],
                 run_dir: Path):
        self.run_id = run_id
        self.workflow = workflow
        self.seed = seed
        self.run_dir = run_dir
        self.completed: Dict[str, Dict[str, Any]] = {}
        self.status = "running"
        self.created_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.timings: Dict[str, float] = {}

    # -- persistence ----------------------------------------------------
    @property
    def _path(self) -> Path:
        return self.run_dir / "run_state.json"

    def save(self) -> None:
        payload = {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "seed": self.seed,
            "run_dir": str(self.run_dir),
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "total_s": self.total_s,
            "timings": self.timings,
            "completed": self.completed,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self._path)

    def mark_completed(self, stage_id: str, outputs: Dict[str, Any]) -> None:
        self.completed[stage_id] = outputs
        self.save()

    def mark_stage_timing(self, stage_id: str, seconds: float) -> None:
        """Record a stage's wall-clock seconds in run_state.json (measurement
        instrumentation, not a pipeline capability — used to answer the
        multi-channel scheduling question with real numbers)."""
        self.timings[stage_id] = round(seconds, 3)
        self.save()

    def mark_done(self) -> None:
        self.status = "done"
        self.completed_at = time.time()
        self.save()

    @property
    def total_s(self) -> Optional[float]:
        """Whole-run wall-clock across every session (created_at -> done)."""
        if self.created_at is None or self.completed_at is None:
            return None
        return round(self.completed_at - self.created_at, 3)

    # -- helpers --------------------------------------------------------
    @staticmethod
    def create(workflow: str, seed: Dict[str, Any]) -> "RunState":
        run_id = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
        run_dir = config.OUTPUT_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state = RunState(run_id, workflow, seed, run_dir)
        state.created_at = time.time()
        state.save()
        return state

    @staticmethod
    def load(run_id: str) -> "RunState":
        """Load an existing run's state for resume."""
        if not _RUN_ID_RE.match(run_id):
            raise FileNotFoundError(
                f"Invalid run id '{run_id}'; expected letters, digits, '-' or '_'")
        for p in config.OUTPUT_DIR.glob(f"{glob.escape(run_id)}/run_state.json"):
            payload = json.loads(p.read_text(encoding="utf-8"))
            return RunState(
                run_id=payload["run_id"],
                workflow=payload["workflow"],
                seed=payload.get("seed", {}),
                run_dir=Path(payload["run_dir"]),
            )._restore(payload)
        raise FileNotFoundError(f"No run state found for run '{run_id}'")

    def _restore(self, payload) -> "RunState":
        self.status = payload.get("status", "running")
        self.completed = dict(payload.get("completed", {}))
        self.created_at = payload.get("created_at")
        self.completed_at = payload.get("completed_at")
        self.timings = dict(payload.get("timings", {}))
        return self
