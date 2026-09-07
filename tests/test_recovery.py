"""R1-F1 / R1-W1: regression tests for the recovery learning path."""
from automato.resilience import recovery


class _FakeLocs:
    def __init__(self):
        self.learned = []

    def learn(self, group, selector):
        self.learned.append((group, selector))


class _FakePage:
    """Minimal page stub. locator() returns a fake whose .count() we control."""

    def __init__(self, id_count=1, role_count=1):
        self._id_count = id_count
        self._role_count = role_count
        self._locator_calls = []

    def locator(self, sel):
        self._locator_calls.append(sel)
        return _FakeLoc(sel, self._id_count)

    def get_by_role(self, role, name=None, exact=False):
        return _FakeLoc(f"get_by_role:{role}:{name}", self._role_count)


class _FakeLoc:
    def __init__(self, sel, count):
        self.sel = sel
        self._count = count

    def count(self):
        return self._count


def test_learn_if_stable_learns_unique_id():
    locs = _FakeLocs()
    page = _FakePage(id_count=1)
    candidate = {"css": "#create-button", "role": "button", "name": "Create"}
    recovery._learn_if_stable(locs, "create", candidate, page)
    assert locs.learned == [("create", "#create-button")]


def test_learn_if_stable_does_not_learn_ambiguous_id():
    locs = _FakeLocs()
    page = _FakePage(id_count=2, role_count=2)  # id AND role both ambiguous
    candidate = {"css": "#create-button", "role": "button", "name": "Create"}
    recovery._learn_if_stable(locs, "create", candidate, page)
    assert locs.learned == []


def test_learn_if_stable_learns_unique_role_name():
    locs = _FakeLocs()
    page = _FakePage(id_count=0, role_count=1)
    candidate = {"css": "button", "role": "link", "name": "Sign in"}
    recovery._learn_if_stable(locs, "signin", candidate, page)
    # id path fails (css is not "#id"); role+name path resolves uniquely.
    assert locs.learned == [("signin", "get_by_role:link:Sign in")]


def test_learn_if_stable_skips_non_identifier_css():
    locs = _FakeLocs()
    page = _FakePage(id_count=1)
    candidate = {"css": "button:nth-of-type(3)", "role": "button", "name": "Go"}
    recovery._learn_if_stable(locs, "go", candidate, page)
    # css is not "#<id>", so id branch is skipped; role+name resolves uniquely.
    assert locs.learned == [("go", "get_by_role:button:Go")]


def test_learn_if_stable_never_raises_on_bad_candidate():
    locs = _FakeLocs()
    page = _FakePage(id_count=1)
    # Simulate an exception inside the function (e.g. lstrip on a non-str).
    recovery._learn_if_stable(locs, "x", {"css": 123}, page)
    # Should not raise; nothing learned.
    assert locs.learned == []
