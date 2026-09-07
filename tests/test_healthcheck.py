"""R3-F1: health-check probe plumbing (no live browser needed for these tests)."""
from automato.healthcheck import _make_probes, _perchance_probe, _probe_group


class _FakeLocator:
    def __init__(self, ok=True):
        self._ok = ok

    @property
    def first(self):
        return self

    def scroll_into_view_if_needed(self, timeout=5000):
        if not self._ok:
            raise TimeoutError("element missing")

    def wait_for(self, state="attached", timeout=5000):
        if not self._ok:
            raise TimeoutError("element missing")

    def count(self):
        return 1 if self._ok else 0


class _FakePage:
    def __init__(self, ok=True):
        self._ok = ok

    def locator(self, sel):
        return _FakeLocator(self._ok)

    def get_by_role(self, role, name=None, exact=False):
        return _FakeLocator(self._ok)


def test_probe_registry_covers_all_providers():
    probes = _make_probes()
    assert [p["name"] for p in probes] == ["youtube", "ai_studio", "tts", "perchance"]
    for p in probes:
        assert p["url"] and p["groups"]


def test_probe_group_ok_and_fail():
    youtube = [p for p in _make_probes() if p["name"] == "youtube"][0]
    ok, detail = _probe_group(_FakePage(ok=True), youtube, "create", "resolve", 3000)
    assert ok is True
    assert detail == "resolved"
    bad, _ = _probe_group(_FakePage(ok=False), youtube, "create", "resolve", 3000)
    assert bad is False


def test_perchance_probe_detects_generator_frame():
    class Frame:
        def __init__(self, url):
            self.url = url

    class Page:
        frames = [Frame("https://perchance.org/ai-text-to-image-generator-embed")]

        def locator(self, sel):
            return _FakeLocator(ok=True)

    ok, detail = _perchance_probe(Page(), timeout_ms=2000)
    assert ok is True
    assert "generator frame present" in detail


def test_perchance_probe_reports_missing_generator():
    class Frame:
        def __init__(self, url):
            self.url = url

    class Page:
        frames = [Frame("https://perchance.org/other"), Frame("https://cdn.example/x")]

        def locator(self, sel):
            return _FakeLocator(ok=True)

    ok, _ = _perchance_probe(Page(), timeout_ms=2000)
    assert ok is False
