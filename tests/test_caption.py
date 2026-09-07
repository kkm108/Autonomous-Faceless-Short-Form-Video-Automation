"""R1-F1 / R1-W8: caption wrapping/truncation tests."""
from automato.adapters.assembly import ffmpeg


class _FakeFont:
    size = 12


def _draw_for():
    """A draw stub whose textlength == len(text), recording drawn texts."""
    class Draw:
        def __init__(self):
            self.drawn = []

        def textlength(self, text, font=None):
            return len(text)

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
