"""Workflow manifest loading and adapter dispatch.

A manifest is a JSON document that lists stages in strict order. Each stage names
an adapter (dotted import path), its input bindings (which output artifact feeds
it) and its output binding (where the artifact is stored).
"""
from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any, Dict

from . import config

log = logging.getLogger(__name__)


class WorkflowError(Exception):
    pass


def load_manifest(name: str) -> Dict[str, Any]:
    path = config.WORKFLOWS_DIR / f"{name}.json"
    if not path.exists():
        raise WorkflowError(f"Workflow not found: {path}")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    _validate_manifest(manifest)
    return manifest


def _validate_manifest(manifest: Dict[str, Any]) -> None:
    """Structural validation: fail fast with clear messages instead of a cryptic
    KeyError mid-run. (#13)"""
    stages = manifest.get("stages")
    if not isinstance(stages, list) or not stages:
        raise WorkflowError("Manifest must declare a non-empty 'stages' list")
    seen = set()
    for i, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise WorkflowError(f"Stage[{i}] is not an object")
        sid = stage.get("id")
        if not sid:
            raise WorkflowError(f"Stage[{i}] is missing an 'id'")
        if sid in seen:
            raise WorkflowError(f"Duplicate stage id: {sid!r}")
        seen.add(sid)
        if not stage.get("adapter"):
            raise WorkflowError(f"Stage '{sid}' is missing an 'adapter'")
        outputs = stage.get("outputs")
        if outputs is not None and not isinstance(outputs, dict):
            raise WorkflowError(f"Stage '{sid}' 'outputs' must be an object")
        if not isinstance(stage.get("inputs", {}), dict):
            raise WorkflowError(f"Stage '{sid}' 'inputs' must be an object")


def stage_output_names(manifest: Dict[str, Any]) -> Dict[str, set]:
    """Map stage id -> the set of output *names* it declares.
    Used to disambiguate dotted artifact refs from literal dotted filenames."""
    out: Dict[str, set] = {}
    for stage in manifest.get("stages", []):
        declared = stage.get("outputs", {})
        if isinstance(declared, dict):
            out[stage.get("id", "")] = set(declared.keys())
    return out


def _import_adapter(dotted: str):
    # dotted like "scripting.generic_llm" -> automato.adapters.scripting.generic_llm.run
    mod_name = f"automato.adapters.{dotted}"
    try:
        module = importlib.import_module(mod_name)
    except ModuleNotFoundError:
        # allow fully-qualified override strings too, e.g. "my.pkg.adapter.run"
        module_path, _, attr = dotted.rpartition(".")
        module = importlib.import_module(module_path)
        return getattr(module, attr or "run")
    return getattr(module, "run")


def adapter_callable(dotted: str):
    return _import_adapter(dotted)
