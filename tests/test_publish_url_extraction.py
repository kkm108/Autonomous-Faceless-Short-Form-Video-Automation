"""Pure-unit tests for the publish URL extraction hardening added after R6
surveillance found the uploader recording a stale video id across several runs.

Covers: structured id parsing from href shapes, title-scoped extraction (the
stale-latch regression), and the known-id collision guard.
"""
import json
from unittest.mock import MagicMock

from automato.adapters.publish.youtube_studio import (
    _extract_id_for_title,
    _id_from_href,
    _known_video_ids,
    _omnisearch_results,
)


def test_id_from_href_variants():
    assert _id_from_href("https://youtu.be/uoXnHJhwM6M") == "uoXnHJhwM6M"
    assert _id_from_href("https://www.youtube.com/watch?v=q8RpQtYoxA4") == "q8RpQtYoxA4"
    assert _id_from_href("https://studio.youtube.com/video/PM2gdm-lpvg/edit") == "PM2gdm-lpvg"
    assert _id_from_href("https://youtube.com/shorts/mVKYZgMupNU") == "mVKYZgMupNU"
    assert _id_from_href("https://www.youtube.com/watch?v=") is None
    assert _id_from_href("") is None
    assert _id_from_href("not a link at all") is None


def _stub_link(href, text="", row_text=""):
    link = MagicMock()
    link.get_attribute.return_value = href
    link.inner_text.return_value = text
    link.evaluate.return_value = row_text
    return link


def _stub_page(link_sets):
    """page.locator(sel) -> a fake locator per selector string."""
    page = MagicMock()

    def _locator(sel):
        links = link_sets.get(sel, [])

        class _Loc:
            def count(self):
                return len(links)

            def nth(self, i):
                return links[i]

        return _Loc()

    page.locator.side_effect = _locator
    return page


def test_extract_id_for_title_skips_stale_first_match():
    # The old behaviour latched onto the FIRST /watch?v link page-wide, which
    # was an older video. Title-scoping must pick the link whose row names the
    # freshly uploaded title instead.
    stale = _stub_link("https://www.youtube.com/watch?v=uoXnHJhwM6M",
                       row_text="Why Ancient Rome Built Straight Roads")
    fresh = _stub_link("https://www.youtube.com/watch?v=q8RpQtYoxA4",
                       row_text="finalWhy Birds Sing at Dawn")
    page = _stub_page({"body a[href*='/watch?v=']": [stale, fresh]})
    vid = _extract_id_for_title(page, "Why Birds Sing at Dawn", scope="body")
    assert vid == "q8RpQtYoxA4"


def test_extract_id_for_title_prefers_upload_dialog_scope():
    dialog = _stub_link("https://youtu.be/ZSxxyD9PVwM",
                        row_text="final5 Proven Daily Habits of Successful People")
    page = _stub_page({"ytcp-uploads-dialog a[href*='youtu.be']": [dialog]})
    vid = _extract_id_for_title(page, "5 Proven Daily Habits of Successful People",
                                scope="ytcp-uploads-dialog")
    assert vid == "ZSxxyD9PVwM"


def test_extract_id_for_title_matches_truncated_row():
    # Studio truncates long titles in row text (e.g. "…" ellipsis); the matcher
    # must work off a safe prefix so a truncated row is still recognized.
    fresh = _stub_link("https://studio.youtube.com/video/q8RpQtYoxA4/edit",
                       row_text="The Shocking Story Behind Your Penc...")
    page = _stub_page({"body a[href*='/video/']": [fresh]})
    vid = _extract_id_for_title(page, "The Shocking Story Behind Your Pencil",
                                scope="body")
    assert vid == "q8RpQtYoxA4"


def test_known_video_ids_collects_prior_runs(tmp_path):
    root = tmp_path / "output"
    for name in ("a_run", "b_run", "this_run"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "a_run" / "post_url.json").write_text(json.dumps(
        {"url": "https://www.youtube.com/watch?v=AAA111aaa11"}), encoding="utf-8")
    (root / "b_run" / "post_url.json").write_text(json.dumps(
        {"url": "https://www.youtube.com/watch?v=BBB222bbb22"}), encoding="utf-8")

    ids = _known_video_ids(exclude_run_dir=root / "this_run", root=root)
    assert ids == {"AAA111aaa11", "BBB222bbb22"}
    ids_self = _known_video_ids(exclude_run_dir=root / "b_run", root=root)
    assert "BBB222bbb22" not in ids_self


def test_known_video_ids_tolerates_garbage(tmp_path):
    root = tmp_path / "output"
    (root / "x").mkdir(parents=True)
    (root / "x" / "post_url.json").write_text("{not json", encoding="utf-8")
    (root / "y").mkdir()
    (root / "y" / "post_url.json").write_text(json.dumps(
        {"status": "uploaded"}), encoding="utf-8")
    assert _known_video_ids(exclude_run_dir=root / "z", root=root) == set()


def _stub_omnisearch_page(eval_rows):
    """A page whose omnisearch panel poll returns ``eval_rows`` and whose other
    page nodes are inert."""
    page = MagicMock()

    class _SearchButton:
        first = MagicMock(click=MagicMock())

        def count(self):
            return 1

    class _Query:
        first = MagicMock(is_visible=MagicMock(return_value=True),
                          fill=MagicMock())

        def count(self):
            return 1

    page.get_by_role = MagicMock(return_value=_SearchButton())
    page.locator = MagicMock(return_value=_Query())
    page.keyboard = MagicMock()
    page.evaluate = MagicMock(return_value=eval_rows)
    return page


def test_omnisearch_results_dedups_by_video_id():
    # Studio renders the SAME uploaded short in several equivalent rows; the
    # tuple-vs-string membership check previously let every duplicate through.
    rows = [
        {"text": "0:15 Find The Hidden Number Before Time Runs Out Sep 9, 2026",
         "href": "https://studio.youtube.com/video/1PWa6DEwflU/edit"},
        {"text": "0:15 Find The Hidden Number Before Time Runs Out Sep 9, 2026",
         "href": "https://studio.youtube.com/video/1PWa6DEwflU/edit"},
        {"text": "0:11 Day 15 of 31 Sep 8, 2026",
         "href": "https://studio.youtube.com/video/lrGZW3FhHGY/edit"},
    ]
    page = _stub_omnisearch_page(rows)
    pairs = _omnisearch_results(page, "Find The Hidden Number")
    assert [(t, vid) for t, vid in pairs] == [
        ("0:15 Find The Hidden Number Before Time Runs Out Sep 9, 2026",
         "1PWa6DEwflU"),
        ("0:11 Day 15 of 31 Sep 8, 2026", "lrGZW3FhHGY"),
    ]
