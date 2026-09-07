"""R1-F1: manifest validation pure-logic tests."""
import pytest

from automato.manifest import WorkflowError, _validate_manifest, stage_output_names


def test_valid_manifest_passes():
    m = {
        "stages": [
            {"id": "a", "adapter": "automato.adapters.scripting.x", "inputs": {}, "outputs": {}},
            {"id": "b", "adapter": "automato.adapters.assembly.x", "inputs": {}, "outputs": {}},
        ]
    }
    _validate_manifest(m)  # should not raise


def test_missing_stages_raises():
    with pytest.raises(WorkflowError):
        _validate_manifest({})


def test_duplicate_stage_ids_raise():
    m = {"stages": [
        {"id": "a", "adapter": "mod", "inputs": {}, "outputs": {}},
        {"id": "a", "adapter": "mod2", "inputs": {}, "outputs": {}},
    ]}
    with pytest.raises(WorkflowError):
        _validate_manifest(m)


def test_missing_adapter_raises():
    m = {"stages": [{"id": "a", "inputs": {}, "outputs": {}}]}
    with pytest.raises(WorkflowError):
        _validate_manifest(m)


def test_non_dict_inputs_raises():
    m = {"stages": [{"id": "a", "adapter": "mod", "inputs": [], "outputs": {}}]}
    with pytest.raises(WorkflowError):
        _validate_manifest(m)


def test_stage_output_names():
    m = {"stages": [
        {"id": "script", "adapter": "x", "inputs": {}, "outputs": {"script": "f.json"}},
        {"id": "publish", "adapter": "y", "inputs": {}, "outputs": {"post_url": "p.json"}},
    ]}
    out = stage_output_names(m)
    assert out == {"script": {"script"}, "publish": {"post_url"}}
