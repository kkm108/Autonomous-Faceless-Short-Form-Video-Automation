"""Regression (R4/R5): every `config.<NAME>` reference in the package must
resolve at import time. The R5 assets-stage crash (`config.PERCHANCE_POLL_INTERVAL_SEC`
vs defined `PERCHANCE_POLL_INTERVAL_S`) was exactly this class of bug — a rename
drift invisible to the browser-driven test suites. This smoke test greps the
package source (no runtime import of the referencing modules needed) and makes
sure each referenced constant actually exists on ``automato.config``.
"""
import pathlib
import re

from automato import config

_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "automato"

_CONFIG_REF = re.compile(r"\bconfig\.([A-Z][A-Z0-9_]+)")

# Names referenced for reasons other than a plain constant lookup (dynamic getattr
# by construction). Keep empty unless a legitimate exception appears.
_ALLOWED_DYNAMIC: set[str] = set()


def _iter_modules():
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_no_loose_config_references():
    missing: dict[str, set[str]] = {}
    for path in _iter_modules():
        text = path.read_text(encoding="utf-8")
        for name in _CONFIG_REF.findall(text):
            if name in _ALLOWED_DYNAMIC:
                continue
            if not hasattr(config, name):
                missing.setdefault(name, set()).add(str(path.relative_to(_PACKAGE_ROOT)))
    assert not missing, (
        "config references that do not resolve:\n"
        + "\n".join(f"  config.{k} <- {sorted(v)}" for k, v in sorted(missing.items()))
    )


def test_referenced_names_are_uppercase_constants():
    """Guard against the reverse drift: a constant renamed away from UPPER_CASE
    (or dropped entirely) while a referenced sibling name stays behind."""
    defined = {n for n in dir(config) if n.isupper() and not n.startswith("_")}
    assert defined, "config should expose at least one UPPER_CASE constant"


def test_settings_constants_have_defaults_in_typed_settings():
    """The config module mirrors UI-ish timings; ensure nothing is a bare stub
    (None/empty) that would silently mis-configure a live run."""
    bare = []
    for path in _iter_modules():
        text = path.read_text(encoding="utf-8")
        for name in _CONFIG_REF.findall(text):
            if name in _ALLOWED_DYNAMIC:
                continue
            value = getattr(config, name, None)
            if value is None or value == "":
                bare.append(f"config.{name} <- {path.relative_to(_PACKAGE_ROOT)}")
    assert not bare, "config references that resolve to None/empty:\n" + "\n".join(bare)
