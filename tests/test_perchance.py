"""R2-W5: pure-logic tests for Perchance prompt/image DOM-container correlation."""

from automato.adapters.assets.perchance_images import (
    _pick_newest_correlated,
    _should_downgrade_scoped,
)


def snap(*frames):
    """Build a FrameSnap from (frame_key, [(hash, b64), ...]) tuples."""
    return list(frames)


def test_no_new_images_returns_empty():
    prev = snap(("0:u", [("a", "A")]))
    cur = snap(("0:u", [("a", "A")]))
    hashes, key = _pick_newest_correlated(prev, cur)
    assert hashes == [] and key is None


def test_single_frame_gains_one_image():
    prev = snap(("0:u", [("a", "A")]))
    cur = snap(("0:u", [("a", "A"), ("b", "B")]))
    hashes, key = _pick_newest_correlated(prev, cur)
    assert hashes == ["b"] and key == "0:u"


def test_newest_generation_frame_wins_when_several_gain():
    # Two embed frames both picked up images in one poll window (e.g. an older
    # generation finishing behind ours). The last frame in DOM order == the newest
    # generation == the one we just submitted.
    prev = snap(("0:u", [("a", "A")]), ("1:u", [("p", "P")]))
    cur = snap(("0:u", [("a", "A"), ("a2", "A2")]),
               ("1:u", [("p", "P"), ("q", "Q")]))
    hashes, key = _pick_newest_correlated(prev, cur)
    assert key == "1:u" and hashes == ["q"]


def test_multi_variant_grid_all_kept_in_dom_order():
    # A newly-created generation container with several finished variants: every
    # new hash is returned, deterministically ordered, not an arbitrary member.
    prev = snap(("0:u", [("a", "A")]))
    cur = snap(("0:u", [("a", "A")]), ("2:u", [("c1", "C1"), ("c2", "C2")]))
    hashes, key = _pick_newest_correlated(prev, cur)
    assert key == "2:u" and hashes == ["c1", "c2"]


def test_new_variant_in_first_frame_when_second_unchanged():
    prev = snap(("0:u", [("a", "A")]), ("1:u", [("p", "P")]))
    cur = snap(("0:u", [("a", "A"), ("b", "B")]), ("1:u", [("p", "P")]))
    hashes, key = _pick_newest_correlated(prev, cur)
    assert key == "0:u" and hashes == ["b"]


def test_empty_scoped_inside_in_flight_window_does_not_downgrade():
    # R5 regression: Perchance clears its canvas between prompts, so immediately
    # after generation the scoped view is legitimately empty for ~30s. Absent any
    # successful scoped read this prompt and inside the in-flight window, an empty
    # snapshot must keep polling scoped -- NOT bail to the page-wide fallback
    # (which previously fired on every single prompt).
    assert _should_downgrade_scoped(False, elapsed_s=5, in_flight_s=75) is False
    assert _should_downgrade_scoped(False, elapsed_s=40, in_flight_s=75) is False


def test_empty_scoped_past_in_flight_window_means_site_shift():
    assert _should_downgrade_scoped(False, elapsed_s=76, in_flight_s=75) is True
    assert _should_downgrade_scoped(False, elapsed_s=150, in_flight_s=75) is True


def test_scoped_images_seen_then_lost_downgrades_immediately():
    # We SAW generation-scoped frames with images this prompt, and now zero remain:
    # that is a real mid-run markup shift, not an in-flight window.
    assert _should_downgrade_scoped(True, elapsed_s=3, in_flight_s=75) is True


def test_per_prompt_target_respects_remaining_budget():
    # R6 batch: "How many" may ask for 4 variants but we should never overshoot
    # the image_count remainder.
    from automato.adapters.assets.perchance_images import _per_prompt_target

    assert _per_prompt_target(6, 4) == 4
    assert _per_prompt_target(2, 4) == 2
    assert _per_prompt_target(6, 1) == 1
    assert _per_prompt_target(6, 0) == 1  # disabled config degrades to single
