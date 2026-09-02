"""Backup, restore, and portability for the engine.

The engine's durable state lives in four places:

  * profiles/<name>/   signed-in browser sessions (cookies, login data) plus the
                       adaptive ``learned.json`` locator overlay
  * output/<run_id>/   every run's artifacts (script, assets, audio, video,
                       post_url, run_state.json)
  * workflows/*.json   the workflow manifests
  * config.py          the engine's current configuration

``build_archive`` bundles all of that into a single relocatable ``.zip``.
``restore_archive`` unpacks it onto this (or another) machine and, critically,
**rewrites absolute paths** recorded inside artifacts so the state works again no
matter where the engine root lives. That path rewriting is what makes the engine
portable: run-state JSON and learned overlays reference the old install directory
on the source machine, and we rebind them to the restore target.

Volatile browser caches / temp dirs (GPUCache, Cache, Crashpad, ShaderCache, ...)
are excluded by default -- they are machine-specific and only bloat the archive.
Auth-relevant browser data (Login Data, Cookies/Network, Local/Session Storage,
Preferences, Secure Preferences, learned.json) is always kept so a restored
install stays signed in and keeps its learned behavior.

Security / robustness notes:
  * Extraction is zip-slip protected: entry names are validated before any write.
  * The manifest records a SHA-256 per archived file; restore verifies every one
    before merging, so a corrupt or tampered archive is refused.
  * The archive format version is validated on restore.
  * Restore is a *destructive overwrite* by design, so it requires ``force=True``
    when the destination already holds data (or the CLI ``--force`` flag).
  * ``apply_config=True`` re-applies the source machine's configuration values to
    the destination ``config.py`` (an idempotent override block), so an altered
    configuration can be reproduced, not just its data.

NOTE: ``.zip`` is an open format. A backup contains live login sessions for the
suite's providers, so treat backups as secrets -- store/encrypt them accordingly.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from . import config

log = logging.getLogger(__name__)

ARCHIVE_MANIFEST = "backup-manifest.json"
CONFIG_SNAPSHOT = "config-snapshot.json"

# Name of the engine-root directory inside the archive.
ROOT_KEY = "automato_state"

FORMAT_VERSION = 1
_SUPPORTED_FORMAT_VERSIONS = (1,)


class RestoreError(Exception):
    """A safety/integrity problem with a restore (bad archive, unsupported
    version, path traversal, corrupt file, or clobber without force)."""


# Config value capture: every module-level, JSON-serializable public attribute
# EXCEPT machine-specific path/dir/file constants and browser executable paths,
# which are not portable across machines.
_CONFIG_SKIP_NAMES = {"ROOT", "STATE_FILE", "BROWSER_EXECUTABLE"}
_CONFIG_RESERVED_SUFFIXES = ("_DIR", "_FILE")


# True if ANY case-insensitive path part *equals* an excluded marker. We compare
# exact basenames (not substrings) so a legit folder simply containing the word
# "cache" (e.g. "header_cache") is never falsely excluded.
_EXCLUDED_DIR_MARKERS = {
    "cache", "cacheddata", "code cache", "gpucache", "gpu cache",
    "grshadercache", "grcache", "shadercache", "shader cache", "dawncache",
    "crashpad", "browsermetrics", "component_crx_cache", "extensions_crx_cache",
    "smart screen", "safebrowsing", "nurturing", "blob_storage", "webstorage",
    "first_party_sets", "shared proto_db", "dips", "jump list",
}
_EXCLUDED_FILENAMES = {
    "lock", "log", "log.old", "favorites_diagnostic.log", "readme.json",
}
_EXCLUDED_PREFIXES = ("browsermetrics-",)
_EXCLUDED_SUFFIXES = (".pma", ".tmp", ".log")


def _want_dir(rel_dir: str) -> bool:
    parts = rel_dir.replace("\\", "/").lower().split("/")
    return not any(p in _EXCLUDED_DIR_MARKERS for p in parts)


def _want_file(name: str) -> bool:
    low = name.lower()
    if low in _EXCLUDED_FILENAMES:
        return False
    if low.startswith(_EXCLUDED_PREFIXES):
        return False
    if low.endswith(_EXCLUDED_SUFFIXES):
        return False
    return True


def _iter_tree(src: Path) -> Iterator[Path]:
    """Yield all files under ``src`` that survive cache/temp filtering."""
    for root, dirs, files in os.walk(str(src)):
        rel_dir = os.path.relpath(root, str(src))
        base = rel_dir if rel_dir != "." else ""
        dirs[:] = [d for d in dirs if _want_dir(os.path.join(base, d) if base else d)]
        dirs.sort()
        for fn in sorted(files):
            if _want_file(fn):
                yield Path(root) / fn


# ---------------------------------------------------------------------------
# Config snapshot ------------------------------------------------------------
# ---------------------------------------------------------------------------
def _config_snapshot() -> dict:
    """Capture every portable (non-path) module-level config attribute."""
    out: Dict[str, object] = {}
    for name in dir(config):
        if name.startswith("_"):
            continue
        if name in _CONFIG_SKIP_NAMES:
            continue
        if name.endswith(_CONFIG_RESERVED_SUFFIXES):
            continue
        value = getattr(config, name)
        if callable(value) or isinstance(value, Path):
            continue
        if isinstance(value, (str, int, float, bool, list, dict, type(None))):
            out[name] = value
    return out


# ---------------------------------------------------------------------------
# Manifest & hashing ---------------------------------------------------------
# ---------------------------------------------------------------------------
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(include_outputs: bool) -> dict:
    import automato
    return {
        "format_version": FORMAT_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_root": str(config.ROOT),
        "root_key": ROOT_KEY,
        "engine_version": getattr(automato, "__version__", "unknown"),
        "include_outputs": include_outputs,
        "hashes": {},  # populated by build_archive
    }


def _default_out_path() -> Path:
    backups = config.ROOT / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    # microseconds avoid same-second collisions across quick successive backups
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1_000_000:06d}"
    return backups / f"automato_backup_{stamp}.zip"


_MODE_NAME = {0x8000: "file", 0x4000: "dir"}


def _is_file_info(info: zipfile.ZipInfo) -> bool:
    """Detect a regular file entry (dirs end with '/' or have the dir flag)."""
    if info.is_dir():
        return False
    mode = info.external_attr >> 16
    mtype = _MODE_NAME.get(mode & 0xF000)
    return mtype == "file" if mtype else not info.filename.endswith("/")


# ---------------------------------------------------------------------------
# build_archive --------------------------------------------------------------
# ---------------------------------------------------------------------------
def build_archive(out_path: Optional[Path] = None,
                  include_outputs: bool = True) -> Path:
    """Bundle profiles, workflows, config, and (optionally) run outputs into a
    portable ``.zip`` and return its path.

    Emits a ``backup-manifest.json`` (with per-file SHA-256) and a
    ``config-snapshot.json`` inside the archive so a restore can validate
    integrity and (optionally) reproduce the source configuration.
    """
    out_path = Path(out_path or _default_out_path()).resolve()
    if out_path.suffix.lower() != ".zip":
        out_path = out_path.with_suffix(".zip")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _warn_if_busy()

    manifest = _manifest(include_outputs)

    sources: List[Tuple[Path, str]] = [
        (config.PROFILES_DIR, f"{ROOT_KEY}/profiles"),
        (config.WORKFLOWS_DIR, f"{ROOT_KEY}/workflows"),
    ]
    if include_outputs:
        sources.append((config.OUTPUT_DIR, f"{ROOT_KEY}/output"))

    # #9: never bundle an archive into its own subtree (generalized guard -- out
    # path living under ANY bundled source would recursively grow forever).
    kept: List[Tuple[Path, str]] = []
    for src, arc_prefix in sources:
        if _is_within(out_path, src):
            log.warning("Skipping %s: backup output lives inside it; "
                        "it will not be included", src.name)
            continue
        kept.append((src, arc_prefix))
    sources = kept

    counts: dict = {}
    with zipfile.ZipFile(str(out_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arc_prefix in sources:
            if not src.exists():
                continue
            n = 0
            for p in _iter_tree(src):
                rel = p.relative_to(src)
                arc = f"{arc_prefix}/{rel.as_posix()}"
                data = p.read_bytes()
                zf.writestr(arc, data)
                manifest["hashes"][arc] = _sha256(data)
                n += 1
            counts[src.name] = n

        counts["config"] = 1
        manifest["counts"] = counts

        snapshot = _config_snapshot()
        zf.writestr(f"{ROOT_KEY}/{CONFIG_SNAPSHOT}",
                    json.dumps(snapshot, ensure_ascii=False, indent=2))
        zf.writestr(f"{ROOT_KEY}/{ARCHIVE_MANIFEST}",
                    json.dumps(manifest, ensure_ascii=False, indent=2))

    log.info("Backup written: %s (%s files)", out_path, sum(counts.values()))
    return out_path


def prune_old_backups(keep: int, backups_dir: Optional[Path] = None) -> List[Path]:
    """Delete all but the newest ``keep`` backups, newest first, by filename (the
    timestamps are ISO-like and sort lexicographically = chronologically).

    Returns the paths that were deleted. A non-positive ``keep`` is a no-op.
    """
    if keep <= 0:
        return []
    backups_dir = Path(backups_dir or (config.ROOT / "backups"))
    if not backups_dir.exists():
        return []
    archives = sorted(
        (p for p in backups_dir.glob("automato_backup_*.zip")),
        key=lambda p: p.name,
        reverse=True,
    )
    if len(archives) <= keep:
        return []
    deleted: List[Path] = []
    for p in archives[keep:]:
        try:
            p.unlink()
            deleted.append(p)
        except OSError as exc:  # noqa: PERF203
            log.warning("Could not delete old backup %s: %s", p, exc)
    return deleted


def _warn_if_busy() -> None:
    """#10: warn when unfinished/in-progress runs exist, so a backup taken
    mid-run is not mistaken for a clean snapshot."""
    try:
        unfinished = 0
        for rs in config.OUTPUT_DIR.glob("*/run_state.json"):
            try:
                payload = json.loads(rs.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if payload.get("status") != "done":
                unfinished += 1
        if unfinished:
            log.warning("%d run(s) are not marked 'done'; a backup taken now may "
                        "capture a partially-written run", unfinished)
    except Exception:  # noqa: BLE001
        pass


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Path rebinding (portability) ----------------------------------------------
# ---------------------------------------------------------------------------
def _norm(p: str) -> str:
    """Normalize separators so matching/rebinding works across OSes."""
    return p.replace("\\", "/").replace("//", "/")


def _rebind(value: str, old_root: str, new_root: str) -> Tuple[str, bool]:
    """Rebind absolute engine-root references inside ``value``.

    Normalizes both roots to '/' for comparison (a Windows archive restored onto
    a POSIX machine, or vice-versa), substitutes every occurrence, then converts
    separators back to the *current* platform so the stored paths are valid here.
    """
    old_n = _norm(old_root)
    new_n = _norm(new_root)
    value_n = _norm(value)
    if old_n not in value_n:
        return value, False
    replaced = value_n.replace(old_n, new_n)
    if os.sep == "\\":
        # back to Windows backslashes
        replaced = replaced.replace("/", "\\")
    return replaced, True


def _rebind_file_text(path: Path, old_root: str, new_root: str) -> bool:
    """Rebind root references in a text file, returning True if changed."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return False
    new_text, changed = _rebind(text, old_root, new_root)
    if changed:
        path.write_text(new_text, encoding="utf-8")
    return changed


# Files we author that may embed absolute engine paths; only these are rebindable
# on restore (browser DB binaries are left untouched to avoid corruption).
_REWRITE_SUFFIXES = (".json", ".txt", ".md", ".csv", ".xml", ".html", ".htm", ".css", ".js")
_REWRITE_FILENAMES = {CONFIG_SNAPSHOT}


def _should_rewrite(p: Path) -> bool:
    return p.suffix.lower() in _REWRITE_SUFFIXES or p.name in _REWRITE_FILENAMES


def _rewrite_json(path: Path, old_root: str, new_root: str) -> bool:
    """Parse a JSON artifact and rebind any string value containing the old
    absolute root to the new root, then re-serialize. Handles JSON backslash
    escaping via structural parsing and a plain .replace()-style rebind."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    changed = [False]

    def walk(node):
        if isinstance(node, str):
            new, flag = _rebind(node, old_root, new_root)
            if flag:
                changed[0] = True
            return new
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    data = walk(data)
    if changed[0]:
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except Exception:  # noqa: BLE001
            return False
        return True
    return False


def _rewrite_root_in_tree(tree_root: Path, old_root: str, new_root: str) -> int:
    """Rebind the absolute source root string to the new root inside every
    artifact (run_state.json, learned.json, post_url.json, ...) for portability.

    JSON artifacts are rewritten structurally (so backslash escaping in stored
    paths is handled correctly); other trusted text files get a raw replacement.
    Binary browser DB files are left untouched so we never corrupt them.
    """
    changed = 0
    for p in tree_root.rglob("*"):
        if not p.is_file() or not _should_rewrite(p):
            continue
        if p.suffix.lower() == ".json":
            ok = _rewrite_json(p, old_root, new_root)
        else:
            ok = _rebind_file_text(p, old_root, new_root)
        if ok:
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# Safe extraction & merge ----------------------------------------------------
# ---------------------------------------------------------------------------
def _validate_entries(names: List[str], root_key: str) -> None:
    """Reject zip-slip/absolute traversal before extracting anything."""
    for name in names:
        if name.startswith("/") or ("\\" in name and name.split("\\", 1)[0] in ("C:", "c:")):
            raise RestoreError(f"Refusing unsafe absolute path in archive: {name!r}")
        norm = name.replace("\\", "/")
        if ".." in norm.split("/"):
            raise RestoreError(f"Refusing path traversal in archive: {name!r}")
        if norm.split("/", 1)[0] != root_key:
            raise RestoreError(f"Refusing entry outside root key: {name!r}")


def _verify_integrity(staging_root: Path, manifest: dict) -> None:
    """#8: verify every hashed file extracted correctly before merge."""
    hashes = manifest.get("hashes") or {}
    for arc, expected in hashes.items():
        f = staging_root / arc
        if not f.is_file():
            raise RestoreError(f"Integrity failure: missing file in archive: {arc}")
        if _sha256(f.read_bytes()) != expected:
            raise RestoreError(f"Integrity failure: hash mismatch: {arc}")


def _merge_copy(src: Path, dst: Path) -> None:
    """Copy every file from ``src`` into ``dst``, overwriting existing files."""
    for root, dirs, files in os.walk(str(src)):
        rel = os.path.relpath(root, str(src))
        target = dst if rel == "." else dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for fn in sorted(files):
            shutil.copy2(Path(root) / fn, target / fn)


# ---------------------------------------------------------------------------
# Config reproduction (apply_config) -----------------------------------------
# ---------------------------------------------------------------------------
_CONFIG_MARK_START = "# === automato restore override (source machine) ==="
_CONFIG_MARK_END = "# === end automato restore override ==="


def _apply_config_to(path: Path, snapshot: dict) -> None:
    """Append an idempotent override block to ``config.py`` re-applying the
    source machine's portable config values. Re-running is safe (old block is
    removed first)."""
    original = ""
    if path.exists():
        original = path.read_text(encoding="utf-8")

    # strip any previous override block so it is idempotent
    start = original.find(_CONFIG_MARK_START)
    end = original.find(_CONFIG_MARK_END)
    if start != -1 and end != -1 and end > start:
        original = original[:start] + original[end + len(_CONFIG_MARK_END):]

    lines = [f"{_CONFIG_MARK_START}"]
    lines.append("# Config values captured from the machine that produced this archive.")
    lines.append("# Safe to delete; here only so restore --apply-config reproduces the source config.")
    for k, v in snapshot.items():
        if k.startswith("_") or k in _CONFIG_SKIP_NAMES or k.endswith(_CONFIG_RESERVED_SUFFIXES):
            continue
        lines.append(f"{k} = {json.dumps(v, ensure_ascii=False)}")
    lines.append(_CONFIG_MARK_END)

    block = "\n".join(lines) + "\n"
    path.write_text((original.rstrip() + "\n\n" + block).lstrip("\n"), encoding="utf-8")


# ---------------------------------------------------------------------------
# restore_archive ------------------------------------------------------------
# ---------------------------------------------------------------------------
def restore_archive(archive: Path, dest_root: Optional[Path] = None,
                    force: bool = False, apply_config: bool = False) -> Path:
    """Extract a backup archive onto the target engine root and rebind absolute
    paths for portability.

    ``dest_root`` is the engine root the state should live under (the parent of
    ``profiles/`` and ``output/``). Defaults to the current engine root; pass a
    different path to restore onto another machine / directory.

    ``force`` permits a destructive overwrite of an already-populated
    destination (restore replaces files by design). Without it, restoring onto a
    non-empty destination raises ``RestoreError``.

    ``apply_config`` re-applies the source machine's portable config values to
    the destination ``config.py`` (idempotent override block), reproducing an
    altered configuration rather than only its data.
    """
    archive = Path(archive).resolve()
    if not archive.exists():
        raise FileNotFoundError(f"Backup archive not found: {archive}")

    dest_root = Path(dest_root or config.ROOT).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)

    if not force and _dest_populated(dest_root):
        raise RestoreError(
            f"Destination {dest_root} is not empty. Restore overwrites existing "
            f"files; pass force=True / --force to confirm you want to replace them."
        )

    staging = Path(config.OUTPUT_DIR) / f".restore_{int(time.time())}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(str(archive), "r") as zf:
            entries = [i for i in zf.infolist() if not i.is_dir()]
            _validate_entries([i.filename for i in entries], ROOT_KEY)
            zf.extractall(str(staging))

        root_dir = staging / ROOT_KEY
        if not root_dir.exists():
            raise RestoreError("Archive does not look like an engine backup "
                               "(missing root key)")

        manifest = {}
        try:
            manifest = json.loads((root_dir / ARCHIVE_MANIFEST).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            raise RestoreError("Archive is missing/unreadable backup-manifest.json")

        # #7: reject future/unknown format versions.
        version = manifest.get("format_version")
        if version is None:
            raise RestoreError("Archive has no format_version; refusing to restore")
        if version not in _SUPPORTED_FORMAT_VERSIONS:
            raise RestoreError(
                f"Unsupported archive format_version={version} "
                f"(supported: {_SUPPORTED_FORMAT_VERSIONS}); "
                f"you need a newer engine version to restore this backup."
            )

        # #8: verify hashes before we touch the destination.
        _verify_integrity(staging, manifest)

        old_root = manifest.get("source_root")
        rewritten = 0
        if old_root and old_root != str(dest_root):
            rewritten = _rewrite_root_in_tree(root_dir, old_root, str(dest_root))

        for sub in ("profiles", "workflows", "output"):
            src = root_dir / sub
            if src.exists():
                _merge_copy(src, dest_root / sub)

        # #2 / #13: optionally reproduce the source config (idempotent).
        # We no longer drop a stray snapshot into the engine root; the values are
        # applied directly to config.py when requested.
        if apply_config:
            snap = root_dir / CONFIG_SNAPSHOT
            if snap.exists():
                snapshot = json.loads(snap.read_text(encoding="utf-8"))
                _apply_config_to(dest_root / "config.py", snapshot)
                log.info("Applied source configuration to %s",
                         dest_root / "config.py")

        moved = str(dest_root) + (f" (portability: {old_root} -> {dest_root})" if rewritten else "")
        log.info("Restore complete into %s; rewrote %d path references", moved, rewritten)
        return dest_root
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _dest_populated(dest_root: Path) -> bool:
    """True when the destination already holds engine state we could clobber."""
    for sub in ("profiles", "workflows", "output"):
        p = dest_root / sub
        if p.exists() and any(p.iterdir()):
            return True
    return False
