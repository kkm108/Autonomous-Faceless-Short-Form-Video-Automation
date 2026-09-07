"""R3-W5/R3-F3: adapter resolution is allowlisted by default."""
import sys
import types

import pytest

from automato import config
from automato.manifest import WorkflowError, adapter_callable


def test_in_tree_adapter_resolves():
    fn = adapter_callable("assembly.ffmpeg")
    assert callable(fn)


def _inject_external_module():
    module = types.ModuleType("automato_ext_test_helper")
    module.run = lambda: "ran"
    sys.modules["automato_ext_test_helper"] = module
    return module


def test_external_adapter_blocked_by_default(monkeypatch):
    _inject_external_module()
    try:
        with pytest.raises(WorkflowError):
            adapter_callable("automato_ext_test_helper.run")
    finally:
        sys.modules.pop("automato_ext_test_helper", None)
    assert config.ALLOW_EXTERNAL_ADAPTERS is False  # default is off


def test_external_adapter_allowed_with_explicit_optin(monkeypatch):
    _inject_external_module()
    monkeypatch.setattr(config, "ALLOW_EXTERNAL_ADAPTERS", True)
    try:
        fn = adapter_callable("automato_ext_test_helper.run")
        assert callable(fn)
        assert fn() == "ran"
    finally:
        sys.modules.pop("automato_ext_test_helper", None)
