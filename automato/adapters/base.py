"""Adapter base contract.

Each stage in the pipeline maps to an adapter that:
  * declares its provider session name (or None if it needs no browser, e.g. FFmpeg),
  * implements ``run(ctx, inputs, run_dir, session)`` and returns a dict of
    outputs that exactly matches the manifest's output contract.

The context object provides: config, logging, and a helper to resolve variables.
"""
from __future__ import annotations

import abc
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .. import config

log = logging.getLogger(__name__)


class ExecutorError(Exception):
    """Typed failure from an executor stage (adapter or its dependencies).

    ``retryable`` tells the engine whether re-running the step is likely to help
    (True for transient issues) or whether it will always fail the same way
    (False for deterministic/contract errors). The orchestrator wraps raw
    dependency exceptions into this type at the adapter boundary (AGENTS.md).
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class AdapterResult:
    def __init__(self, outputs: Dict[str, Any], elapsed_s: float = 0.0):
        self.outputs = outputs
        self.elapsed_s = elapsed_s


class BaseAdapter(abc.ABC):
    #: provider profile to use; None = no browser needed (local compute).
    provider: Optional[str] = None
    #: human-readable name of the adapter.
    name: str = "base"

    @abc.abstractmethod
    def run(self, ctx: "AdapterContext", inputs: Dict[str, Any],
            run_dir: Path, session=None) -> Dict[str, Any]:
        """Execute the stage returning the stage's output dict."""

    # Convenience: resolve seeded variables embedded in inputs (handled by
    # orchestrator, so adapters normally receive already-resolved values).


class AdapterContext:
    """Ordinary object that carries runtime context to adapters (config, seed,
    run dir, logger). Kept simple to avoid circular imports."""

    def __init__(self, seed: Dict[str, Any], workflow: dict):
        self.seed = seed
        self.workflow = workflow
        self.global_config = config
