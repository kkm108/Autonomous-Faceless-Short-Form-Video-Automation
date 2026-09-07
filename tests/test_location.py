"""R3-W2/R3-F2/R3-W3: learned-overlay lifecycle and candidate retry in location.py."""
import json
from datetime import datetime, timedelta

import pytest

from automato import config
from automato.resilience.location import ProviderLocations


def _write(tmp_path, data):
    f = tmp_path / "learned.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


class _Loc:
    """A resolved locator. ``fail_scrolls`` = attempts that raise before success
    (e.g. 1 = a transient failure on the first try, then success; 100 = always)."""

    def __init__(self, attached=True, count=1, fail_scrolls=0):
        self._attached = attached
        self._count = count
        self._fail_scrolls = fail_scrolls
        self.tries = 0

    def scroll_into_view_if_needed(self, timeout=5000):
        self.tries += 1
        if self.tries <= self._fail_scrolls:
            raise TimeoutError("transient scroll timeout (retryable)")
        return None

    def wait_for(self, state="attached", timeout=5000):
        self.tries += 1
        if not self._attached:
            raise TimeoutError("element not attached")
        return None

    def count(self):
        return self._count


class _Missing:
    def scroll_into_view_if_needed(self, timeout=5000):
        raise TimeoutError("no such element")

    def wait_for(self, state="attached", timeout=5000):
        raise TimeoutError("no such element")

    def count(self):
        return 0


class _First:
    def __init__(self, loc):
        self._loc = loc

    @property
    def first(self):
        return self._loc


class _Page:
    def __init__(self, locs):
        self._locs = locs  # selector -> locator-or-_Missing
        self.accessed = []

    def locator(self, sel):
        self.accessed.append(sel)
        return _First(self._locs.get(sel, _Missing()))

    def get_by_role(self, role, name=None, exact=False):
        sel = f"get_by_role:{role}:{name}"
        return self.locator(sel)


def test_learn_stamps_record_with_metadata(tmp_path):
    f = tmp_path / "learned.json"
    locs = ProviderLocations({"go": ["#static-go"]}, learned_file=f)
    locs.learn("go", "#got-it", origin="recovery")
    data = json.loads(f.read_text(encoding="utf-8"))
    rec = data["go"][0]
    assert rec["selector"] == "#got-it"
    assert rec["origin"] == "recovery"
    assert rec["learned_at"] is not None
    assert rec["static_hits_since"] == 0
    # learned first, static after
    assert locs.candidates("go") == ["#got-it", "#static-go"]


def test_legacy_bare_string_format_migrates(tmp_path):
    f = _write(tmp_path, {"go": ["#old-one", "#old-two"]})
    locs = ProviderLocations({"go": ["#static-go"]}, learned_file=f)
    assert locs.candidates("go") == ["#old-one", "#old-two", "#static-go"]


def test_expired_learned_entries_are_dropped(tmp_path):
    old = (datetime.now() - timedelta(days=config.LEARNED_SELECTOR_TTL_DAYS + 1))
    f = _write(tmp_path, {"go": [{"selector": "#stale", "learned_at": old.isoformat(),
                                  "origin": "recovery", "static_hits_since": 0}]})
    locs = ProviderLocations({"go": ["#static-go"]}, learned_file=f)
    assert locs.candidates("go") == ["#static-go"]
    data = json.loads(f.read_text(encoding="utf-8"))
    assert "go" not in data or data["go"] == []


def test_static_hits_expire_learned_overlay(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LEARNED_STATIC_HITS_TO_EXPIRE", 3)
    f = tmp_path / "learned.json"
    locs = ProviderLocations({"go": ["#static-go"]}, learned_file=f)
    locs.learn("go", "#learned-go")
    assert locs.candidates("go") == ["#learned-go", "#static-go"]
    for _ in range(3):
        locs.note_static_success("go")
    # overlay dropped: only static remains
    assert locs.candidates("go") == ["#static-go"]


def test_resolve_retries_current_candidate_on_transient_exception(tmp_path):
    locs = ProviderLocations({"go": ["#static-go"]}, learned_file=tmp_path / "j.json")
    learned = _Loc(fail_scrolls=100)  # never succeeds -> fall to static
    static = _Loc(fail_scrolls=1)     # transient: fails once, then retried same
    page = _Page({"#learned-go": learned, "#static-go": static})
    locs._learned["go"] = [{"selector": "#learned-go", "learned_at": None,
                            "origin": "legacy", "static_hits_since": 0}]
    el = locs.resolve(page, "go")
    assert el is static
    assert static.tries == 2          # failed once, then retried same candidate
    assert learned.tries == 2


def test_resolve_moves_on_after_all_attempts_fail(tmp_path):
    locs = ProviderLocations({"go": ["#static-go"]}, learned_file=tmp_path / "j.json")
    page = _Page({"#learned-go": _Loc(fail_scrolls=100),
                  "#static-go": _Loc(fail_scrolls=100)})
    locs._learned["go"] = [{"selector": "#learned-go", "learned_at": None,
                            "origin": "legacy", "static_hits_since": 0}]
    with pytest.raises(LookupError):
        locs.resolve(page, "go")
    assert page.accessed.count("#learned-go") == 2
    assert page.accessed.count("#static-go") == 2


def test_static_success_counts_on_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LEARNED_STATIC_HITS_TO_EXPIRE", 2)
    locs = ProviderLocations({"go": ["#static-go"]}, learned_file=tmp_path / "j.json")
    page = _Page({"#static-go": _Loc(), "#learned-go": _Loc(fail_scrolls=100)})
    locs._learned["go"] = [{"selector": "#learned-go", "learned_at": None,
                            "origin": "legacy", "static_hits_since": 0}]
    for _ in range(2):
        locs.resolve(page, "go")
    assert locs.candidates("go") == ["#static-go"]
