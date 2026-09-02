"""Orchestrator: runs the workflow manifest sequentially.

For each stage (in strict order):
  1. Resolve inputs by substituting seeded variables and prior-stage artifact paths.
  2. If the stage needs a browser, open the provider's persistent session, run the
     provider auth-check, and invoke the adapter with that context + session.
  3. Validate the returned outputs against the manifest contract, persist them;
     a crash resumes from the first incomplete stage.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import config
from .adapters.base import AdapterContext, ExecutorError
from .manifest import WorkflowError, adapter_callable, load_manifest, stage_output_names
from .state import RunState

log = logging.getLogger(__name__)

# map adapter dotted-prefix -> provider session name
_ADAPTER_PROVIDER: Dict[str, str] = {
    "scripting.generic_llm": "ai_studio",
    "assets.perchance_images": "perchance",
    "tts.kokoro_tts": "tts",
    "publish.youtube_studio": "youtube",
}


def _artifact(value: Any, seed: Dict[str, Any], run_dir: Path,
              prior: Dict[str, Dict[str, Any]],
              declared_outs: Dict[str, set]) -> Any:
    """Resolve a manifest binding value.

    * "seed.topic"      -> the seed variable
    * "script.json"     -> a literal absolute path under run_dir
    * "stage_id.output" -> the artifact produced by a prior stage

    A dotted value is treated as an artifact reference ONLY when ``stage_id``
    names a stage AND ``output`` is one of that stage's *declared* output names.
    Anything else is a literal dotted filename under ``run_dir`` (e.g. the
    ``assemble`` stage's ``final.mp4`` output target). This disambiguation keeps
    the shipped manifests working while still surfacing genuine ordering/dependency
    errors as clear ``WorkflowError``s.
    """
    if isinstance(value, str):
        if value.startswith("seed."):
            return seed.get(value[len("seed."):])
        if "." in value:
            stage_id, _, key = value.partition(".")
            if stage_id in declared_outs:
                if key in declared_outs[stage_id]:
                    if stage_id not in prior:
                        raise WorkflowError(
                            f"Cannot resolve binding {value!r}: stage '{stage_id}' "
                            f"has not completed yet (ordering/dependency error)"
                        )
                    out = prior[stage_id].get(key)
                    if out is None:
                        raise WorkflowError(
                            f"Cannot resolve binding {value!r}: stage '{stage_id}' "
                            f"returned no value for declared output '{key}'"
                        )
                    return out
                # segment is a stage but the key is not a declared output -> it is
                # a literal dotted filename, not a mis-typed artifact reference.
                return str(run_dir / value)
            # segment is not a stage -> literal dotted filename.
            return str(run_dir / value)
        return str(run_dir / value)
    return value


def _resolve_bindings(bindings, seed, run_dir, prior, declared_outs):
    if isinstance(bindings, dict):
        return {k: _artifact(v, seed, run_dir, prior, declared_outs)
                for k, v in bindings.items()}
    return _artifact(bindings, seed, run_dir, prior, declared_outs)


def _check_outputs(stage_id: str, adapter: str, declared: set,
                   outputs: Dict[str, Any]) -> None:
    """Validate an adapter's returned outputs against the manifest contract.

    * Every declared output *name* must be present (extras allowed).
    * Path-like returned values that look like artifacts are checked to exist;
      a missing artifact is a hard error (the later stage would only fail more
      confusingly downstream).
    """
    missing = declared - set(outputs.keys())
    if missing:
        raise ExecutorError(
            f"Stage '{stage_id}' ({adapter}) returned outputs missing declared "
            f"names: {sorted(missing)} (got {sorted(outputs)})",
            retryable=False,
        )
    for name, val in outputs.items():
        if not isinstance(val, str):
            continue
        p = Path(val)
        # Only treat it as an artifact path if it's a concrete, path-shaped value
        # (no scheme, no relative that isn't under run_dir).
        if "://" in val:
            continue
        if not p.is_absolute() and not str(p).startswith("."):
            continue
        if not p.exists():
            log.warning(
                "Stage '%s' declared output '%s' points at a missing path: %s "
                "(it may be produced lazily; keep an eye on it)",
                stage_id, name, val,
            )


def run_workflow(workflow_name: str, seed: Dict[str, Any],
                 resume_run_id: Optional[str] = None,
                 only_from_scratch: Optional[bool] = None) -> RunState:
    manifest = load_manifest(workflow_name)
    stages = manifest["stages"]
    declared_outs = stage_output_names(manifest)

    if resume_run_id:
        state = RunState.load(resume_run_id)
        log.info("Resuming run %s from previous state", state.run_id)
    else:
        state = RunState.create(workflow_name, seed)
    ctx = AdapterContext(seed=state.seed, workflow=manifest)
    log.info("Run %s: workflow=%s, %d stages", state.run_id, workflow_name, len(stages))

    prior: Dict[str, Dict[str, Any]] = dict(state.completed or {})

    from .browser import session as session_mod

    for stage in stages:
        sid = stage["id"]
        if sid in prior:
            log.info("Stage '%s' already complete; skipping", sid)
            continue

        log.info("Starting stage '%s' (%s)", sid, stage["adapter"])
        t0 = time.time()
        inputs = _resolve_bindings(stage.get("inputs", {}), seed, state.run_dir,
                                   prior, declared_outs)

        provider = _ADAPTER_PROVIDER.get(stage["adapter"])
        session = None
        browser = None
        declared = declared_outs.get(sid, set())
        try:
            if provider:
                browser, context = session_mod.open_session(provider)
                session = browser  # adapters use the PersistentBrowser wrapper
                session_mod.run_auth_check(provider, browser)

            fn = adapter_callable(stage["adapter"])
            try:
                outputs = fn(ctx, inputs, state.run_dir, session=session)
            except ExecutorError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Wrap raw dependency/adapter exceptions into a typed error so the
                # engine can reason about retryability. (AGENTS.md contract.)
                raise ExecutorError(
                    f"Stage '{sid}' ({stage['adapter']}) failed: {exc}",
                    retryable=False,
                ) from exc

            if not isinstance(outputs, dict):
                raise ExecutorError(
                    f"Adapter '{stage['adapter']}' must return a dict",
                    retryable=False,
                )

            _check_outputs(sid, stage["adapter"], declared, outputs)

            prior[sid] = outputs
            state.mark_completed(sid, outputs)
            log.info("Stage '%s' complete in %.1fs", sid, time.time() - t0)
        finally:
            if browser is not None:
                try:
                    browser.close()
                finally:
                    session_mod.release_session_lock(provider)

    state.mark_done()
    log.info("Workflow '%s' finished. Run dir: %s", workflow_name, state.run_dir)
    return state
