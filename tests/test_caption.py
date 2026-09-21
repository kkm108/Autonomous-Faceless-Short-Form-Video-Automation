"""R1-F1 / R1-W8: caption wrapping/truncation tests."""
from PIL import Image, ImageDraw

from automato.adapters.assembly import ffmpeg


class _FakeFont:
    size = 12


def _draw_for():
    """A draw stub whose textlength == textbbox == len(text), recording drawn
    texts (textbbox mirrors a plain proportional pen with no overhanging ink)."""
    class Draw:
        def __init__(self):
            self.drawn = []

        def textlength(self, text, font=None):
            return len(text)

        def textbbox(self, xy, text, font=None):
            return (xy[0], xy[1], xy[0] + len(text), xy[1] + 20)

        def text(self, pos, text, font=None, fill=None):
            self.drawn.append(text)

    return Draw()


def _render_drawn(caption, box_w):
    draw = _draw_for()
    ffmpeg._draw_text_wrapped(draw, caption, box_w, 500, _FakeFont(), (255, 255, 255))
    return draw.drawn


def test_short_caption_renders_unchanged():
    drawn = _render_drawn("Hello world", box_w=30)
    assert drawn and drawn[0] == "Hello world"


def test_overflow_keeps_start_drops_end():
    text = "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10"
    drawn = _render_drawn(text, box_w=10)
    lines = drawn[::2]  # each line drawn twice: shadow then main text
    assert len(lines) <= 3
    assert lines[0].startswith("w1")
    assert lines[-1].endswith("\u2026")


def test_no_truncation_within_budget():
    drawn = _render_drawn("one two three", box_w=100)
    assert drawn and drawn[0] == "one two three"


def test_empty_text_no_lines():
    assert _render_drawn("", box_w=100) == []


def test_long_unbreakable_token_never_clips():
    # R10-P0: a hyphenated/slug token wider than the box used to be drawn as one
    # centered line at a negative x and clipped at the frame edge (reproduced:
    # 3727px in a 960px box). It must now be hard-broken so every drawn line
    # fits the caption box.
    token = ("bitcoin-halving-2026-explained-in-ten-words-keep-reading-"
             "and-learn-more-about")
    draw = _draw_for()
    box_w = 30
    fit = ffmpeg._draw_text_wrapped(draw, token, box_w, 500, _FakeFont(),
                                    (255, 255, 255))
    lines = draw.drawn[::2]
    assert lines
    for ln in lines:
        assert len(ln) <= box_w, f"line overflows box: {ln!r} ({len(ln)}>{box_w})"
    assert fit["fit"] is True
    assert fit["max_line_px"] <= box_w


def test_caption_fit_metrics_reported():
    # R10-P3: the render returns the fit evidence the assembly self-check needs.
    draw = _draw_for()
    fit = ffmpeg._draw_text_wrapped(draw, "a perfectly fitting line", 40, 500,
                                    _FakeFont(), (255, 255, 255))
    assert fit["lines"] == 1
    assert fit["fit"] is True
    assert fit["max_line_px"] <= fit["box_w"]
    assert fit["block_px"] <= fit["box_h"]


def test_wrap_keeps_start_and_ellipsis_across_truncation():
    text = "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10"
    draw = _draw_for()
    ffmpeg._draw_text_wrapped(draw, text, 10, 500, _FakeFont(), (255, 255, 255))
    lines = draw.drawn[::2]
    assert len(lines) <= 3
    assert lines[0].startswith("w1")
    assert lines[-1].endswith("\u2026")


# ---------------------------------------------------------------------------
# R11-F1: ink-bounding-box fit (left-edge clipping regression)
# ---------------------------------------------------------------------------

class _OverhangDraw:
    """A draw whose text LAYS INK left of the pen origin: textlength reports the
    advance (len), but textbbox reports ink straddling -18..len+18 — emulating a
    heavy leading stroke/left bearing that the advance width never counts."""

    def textlength(self, text, font=None):
        return len(text)

    def textbbox(self, xy, text, font=None):
        return (xy[0] - 18, xy[1], xy[0] + len(text) + 18, xy[1] + 20)


def test_fit_metrics_uses_ink_width_not_advance():
    # A near-box-width line whose ADVANCE fits (len=940 <= 960) but whose INK plus
    # the widened frame margin does not (976 + 2*72 > 1080) must be flagged unfit
    # so the shrink ladder keeps it out of the frame edge.
    line = "W" * 940
    fit = ffmpeg._fit_metrics(_OverhangDraw(), _FakeFont(), [line], 960, 700)
    assert fit["max_line_px"] <= fit["box_w"]      # old advance gate passed
    assert fit["max_ink_px"] == 976
    assert fit["fit"] is False                      # ink+margin gate fails
    # A comfortably tight line still fits (ink 836 + 2*72 = 980 <= 1080).
    ok = ffmpeg._fit_metrics(_OverhangDraw(), _FakeFont(), ["W" * 800], 960, 700)
    assert ok["fit"] is True


class _RecordingOverhangDraw(_OverhangDraw):
    """_OverhangDraw that also records where each line was drawn."""

    def __init__(self):
        self.drawn = []

    def text(self, pos, text, font=None, fill=None):
        self.drawn.append((pos, text))


def test_line_placement_keeps_ink_inside_frame_floor():
    # With a non-resizeable font the ladder cannot shrink an over-wide line, so
    # the placement clamp is the last line of defence: the drawn ink must still
    # sit inside [CAPTION_FLOOR_X, W-CAPTION_FLOOR_X].
    draw = _RecordingOverhangDraw()
    line = "W" * 940
    fit = ffmpeg._draw_text_wrapped(draw, line, 960, 500, _FakeFont(),
                                    (255, 255, 255))
    assert fit["fit"] is False          # cannot fit even at the floor
    (x, y), text = draw.drawn[0]
    assert text == line
    assert x - 18 >= ffmpeg.CAPTION_FLOOR_X
    assert x + 940 + 18 <= ffmpeg.W - ffmpeg.CAPTION_FLOOR_X


def test_live_caption_ink_stays_in_frame_safe_area():
    # The three exact captions that clipped at the LEFT EDGE of the
    # 'Why Your Keyboard Is Built to Slow You Down' video (run 1789456633_98f7d1,
    # slide 0 'Why is your keyboard' had ink 2px from the frame edge). Each must
    # now render with a real safety margin from every frame edge.
    slides = [("Why is your keyboard so random?", 92, 960, 700),
              ("A century-old design that stuck", 92, 960, 700)]
    thumb = [("Why Your Keyboard Is Built to Slow You Down", 84, 940, 560)]
    for text, size, box_w, box_h in slides + thumb:
        im = Image.new("RGB", (ffmpeg.W, ffmpeg.H), (0, 0, 0))
        draw = ImageDraw.Draw(im)
        fit = ffmpeg._draw_text_wrapped(draw, text, box_w, box_h,
                                        ffmpeg._load_font(size),
                                        (255, 255, 255))
        assert fit["fit"] is True, text
        assert fit["max_ink_px"] + 2 * ffmpeg.CAPTION_MARGIN_X <= ffmpeg.W, text
        bb = im.getbbox()
        assert bb is not None, text
        assert bb[0] >= ffmpeg.CAPTION_FLOOR_X, f"ink hits left edge: {text!r}"
        assert bb[2] <= ffmpeg.W - ffmpeg.CAPTION_FLOOR_X, f"ink hits right edge: {text!r}"
