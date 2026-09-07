"""R2-W5: pure-logic tests for Perchance prompt/image DOM-container correlation."""
from automato.adapters.assets.perchance_images import _pick_newest_correlated


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
