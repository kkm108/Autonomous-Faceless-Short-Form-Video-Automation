"""Adapter contract (the engine's AGENTS.md).

Every pipeline stage maps to an adapter module under ``automato/adapters/`` that
implements a bare module-level ``run`` function:

    def run(ctx, inputs, run_dir, session=None) -> dict

Contract rules:
  * ``ctx`` is an :class:`AdapterContext` (carries seed, workflow, validated
    per-run ``settings``, and the config module); ``inputs`` are already resolved
    by the orchestrator (seeded variables and prior-stage artifact paths become
    concrete values); ``run_dir`` is where the stage writes its artifacts;
    ``session`` is the provider's persistent browser (None for local-compute
    stages like FFmpeg).
  * Prefer ``ctx.settings`` (typed, validated :class:`RunSettings`, R2-W2/R2-F2)
    for anything that varies per run (visibility, TTS provider, browser/headless,
    recovery) rather than reading the shared ``config`` module.
  * The return value MUST be a dict whose keys exactly match the manifest's
    declared outputs for that stage (extras are allowed; a missing declared name
    is an error).
  * A stage that needs a browser maps to a provider session via the
    ``_ADAPTER_PROVIDER`` table in ``orchestrator.py``.
  * On failure, raise :class:`ExecutorError` with ``retryable`` set truthfully:
    True only for transient conditions where re-running the stage is likely to
    help (network, rate-limit, in-flight upload); False for deterministic errors
    (contract violations, missing inputs). The orchestrator wraps raw dependency
    exceptions into ``ExecutorError(retryable=False)`` at the adapter boundary,
    and re-attempts ``retryable=True`` stages with backoff before failing.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .. import config
from ..settings import RunSettings

log = logging.getLogger(__name__)


class ExecutorError(Exception):
    """Typed failure from an executor stage (adapter or its dependencies).

    ``retryable`` tells the engine whether re-running the step is likely to help
    (True for transient issues) or whether it will always fail the same way
    (False for deterministic/contract errors). The orchestrator reads this flag to
    drive stage-level retries and wraps raw dependency exceptions into this type
    at the adapter boundary.
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class AdapterContext:
    """Carries runtime context to adapters: validated per-run ``settings``
    (R2-W2/R2-F2), the seed variables, and the workflow manifest.

    ``global_config`` remains available as a stable back-compat alias for the
    ``config`` module (constants/timings); per-run values live on
    ``self.settings``."""

    def __init__(self, seed: Dict[str, Any], workflow: dict,
                 settings: Optional[RunSettings] = None):
        self.seed = seed
        self.workflow = workflow
        self.settings = settings or RunSettings.defaults()
        self.global_config = config
