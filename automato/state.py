"""Run-state persistence and resume.

State is a small JSON ledger under output/state.json keyed by run id. It records
which stages completed and where each artifact lives, so a crashed run can resume
at the first incomplete stage rather than starting over or requiring a human to
bridge data.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from . import config

log = logging.getLogger(__name__)


class RunState:
    def __init__(self, run_id: str, workflow: str, seed: Dict[str, Any],
                 run_dir: Path):
        self.run_id = run_id
        self.workflow = workflow
        self.seed = seed
        self.run_dir = run_dir
        self.completed: Dict[str, Dict[str, Any]] = {}
        self.status = "running"

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

    def mark_done(self) -> None:
        self.status = "done"
        self.save()

    # -- helpers --------------------------------------------------------
    @staticmethod
    def create(workflow: str, seed: Dict[str, Any]) -> "RunState":
        run_id = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
        run_dir = config.OUTPUT_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state = RunState(run_id, workflow, seed, run_dir)
        state.save()
        return state

    @staticmethod
    def load(run_id: str) -> "RunState":
        """Load an existing run's state for resume."""
        for p in config.OUTPUT_DIR.glob(f"{run_id}/run_state.json"):
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
        return self
