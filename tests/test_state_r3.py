"""R3-W6: RunState.load() must not let a user-supplied --resume id shape a
filesystem pattern unsafely."""
import pytest

from automato import config
from automato.state import RunState


def test_load_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="Invalid run id"):
        RunState.load("..")
    with pytest.raises(FileNotFoundError, match="Invalid run id"):
        RunState.load("a/b")


def test_load_rejects_glob_metachars(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="Invalid run id"):
        RunState.load("1700000000_abcde[0]")
    with pytest.raises(FileNotFoundError, match="Invalid run id"):
        RunState.load("1700000000_*")


def test_load_valid_id_gets_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        RunState.load("1700000000_abc123")
    assert "No run state found" in str(excinfo.value)
