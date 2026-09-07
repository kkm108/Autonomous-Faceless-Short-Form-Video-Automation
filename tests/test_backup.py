"""R1-F1: backup portability pure-logic tests (path rebinding / retention)."""
import tempfile
from pathlib import Path

from automato import backup as bk


def test_norm_unifies_separators():
    assert bk._norm(r"C:\A\B") == "C:/A/B"
    assert bk._norm("C://A//B") == "C:/A/B"


def test_rebind_replaces_root():
    import os
    expected = "D:/new/engine"
    if os.sep == "\\":
        expected = expected.replace("/", "\\")
    out, changed = bk._rebind(r"C:\old\engine", r"C:\old\engine", r"D:\new\engine")
    assert changed is True
    assert expected in out


def test_rebind_no_change_when_root_absent():
    out, changed = bk._rebind(r"E:\elsewhere", r"C:\old\engine", r"D:\new")
    assert changed is False
    assert out == r"E:\elsewhere"


def test_rebind_handles_nested_json_value():
    import os
    value = r"C:\old\engine\output\run\run_state.json"
    out, changed = bk._rebind(value, r"C:\old\engine", r"/srv/engine")
    assert changed is True
    expected = "/srv/engine/output/run/run_state.json"
    if os.sep == "\\":
        expected = expected.replace("/", "\\")
    assert out == expected


def test_prune_old_backups_keeps_newest_n():
    with tempfile.TemporaryDirectory() as d:
        names = [
            "automato_backup_20260801_000000_000000.zip",
            "automato_backup_20260802_000000_000000.zip",
            "automato_backup_20260803_000000_000000.zip",
        ]
        for n in names:
            (Path(d) / n).write_text("x", encoding="utf-8")
        deleted = bk.prune_old_backups(2, backups_dir=Path(d))
        assert len(deleted) == 1
        assert deleted[0].name == names[0]
        remaining = sorted(p.name for p in Path(d).glob("automato_backup_*.zip"))
        assert remaining == [names[1], names[2]]


def test_prune_old_backups_noop_for_zero_or_within_keep():
    with tempfile.TemporaryDirectory() as d:
        for n in ("automato_backup_20260801_000000_000000.zip",
                  "automato_backup_20260805_000000_000000.zip"):
            (Path(d) / n).write_text("x", encoding="utf-8")
        assert bk.prune_old_backups(0, backups_dir=Path(d)) == []
        assert bk.prune_old_backups(5, backups_dir=Path(d)) == []


def test_prune_old_backups_noop_when_dir_missing():
    with tempfile.TemporaryDirectory() as d:
        assert bk.prune_old_backups(2, backups_dir=Path(d) / "nope") == []


def test_archive_is_encrypted_roundtrip(tmp_path):
    """R2-W7: build an encrypted archive, detect it, and restore it (hash checks
    and merge) using an explicit passphrase. Uses a tiny isolation by pointing the
    backup at scratch profile/workflow dirs."""
    import json

    from automato import backup as bk

    profiles = tmp_path / "profiles"
    (profiles / "youtube").mkdir(parents=True)
    (profiles / "youtube" / "Cookies").write_bytes(b"fake-cookie-bytes")

    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "test.json").write_text(json.dumps({"stages": []}), encoding="utf-8")

    # Point config at these scratch dirs, restore original afterward.
    orig_p, orig_w = bk.config.PROFILES_DIR, bk.config.WORKFLOWS_DIR
    bk.config.PROFILES_DIR, bk.config.WORKFLOWS_DIR = profiles, wf
    archive = tmp_path / "enc.zip"
    try:
        bk.build_archive(out_path=archive, include_outputs=False,
                         encrypt=True, passphrase="hunter2")
        assert bk._archive_is_encrypted(str(archive)) is True

        dest = tmp_path / "restored"
        bk.restore_archive(archive, dest_root=dest, force=True, passphrase="hunter2")
        assert (dest / "profiles" / "youtube" / "Cookies").exists()
        assert (dest / "workflows" / "test.json").exists()
    finally:
        bk.config.PROFILES_DIR, bk.config.WORKFLOWS_DIR = orig_p, orig_w


def test_build_archive_plain_with_no_encrypt_is_not_detected_as_encrypted(tmp_path):
    import json

    from automato import backup as bk

    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "test.json").write_text(json.dumps({"stages": []}), encoding="utf-8")
    orig_w = bk.config.WORKFLOWS_DIR
    bk.config.WORKFLOWS_DIR = wf
    archive = tmp_path / "plain.zip"
    try:
        bk.build_archive(out_path=archive, include_outputs=False,
                         encrypt=False)
        assert bk._archive_is_encrypted(str(archive)) is False
    finally:
        bk.config.WORKFLOWS_DIR = orig_w
