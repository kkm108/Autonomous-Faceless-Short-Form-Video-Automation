"""R7: Ask Studio topic-ideation — topic parsing against real captured replies
and refusal detection."""

from automato.adapters.ideation.ask_studio import _is_refusal, parse_topic

# A real Ask Studio answer captured live from AI.Powered.Skills (R7 probe). The
# body text is newline-shaped (the probe log flattened '\n' to '.', so the
# header lines here end where the real innerText had a newline). The
# "Recommended Concrete Topic" block names the concrete title on its own line.
REAL_IDEATED_REPLY = (
    "Data & Audience Behavior Insights.\n"
    "High Viewer Retention on Interactive Formats:\n"
    "Your ultra-short puzzle and reflex formats consistently outperform longer "
    "storytelling formats.\n"
    "Recommended Concrete Topic.\n"
    "Recent high engagement on interactive number puzzles.\n"
    "Fast Paced Stop The Timer Visual Reflex Number Challenge.\n"
    "This rapid interactive format capitalizes on your channel's highest "
    "retention and comment participation patterns to drive maximum viewer "
    "completion and repeat loops.\n"
    "Tell me more.\n"
    "Audience Interest: Very likely to be of high interest, backed directly by "
    "your top-performing view durations, replay loops, and comment engagement "
    "across recent challenge Shorts.\n"
    "AI can make mistakes."
)

REAL_SUMMARY_REPLY = (
    "Here is a summary of the performance for your latest Short. Performance "
    "Overview. Metric. Current Value. Benchmark Comparison. Views. 5. Above "
    "typical. Stayed to Watch. 20.0%. Above typical. What Is Performing Well. "
    "Initial Hook & Click/Swipe Retention: A 20% stayed to watch rate is pacing "
    "above your channel's typical baseline. The opening concept is strong enough "
    "to stop some viewers from immediately swiping away. What Needs Improvement. "
    "Viewer Drop-off & Mid-Video Retention: With an average view duration of 5 "
    "seconds on a 63-second Short, viewers are dropping off shortly after the "
    "opening line."
)


def test_parse_topic_from_ideated_reply():
    topic = parse_topic(REAL_IDEATED_REPLY)
    assert topic == "Fast Paced Stop The Timer Visual Reflex Number Challenge"


def test_parse_topic_inline_on_marker_line():
    reply = ("Based on your audience, the strongest next move is a replay-loop "
             "format.\nRecommended Concrete Topic: Quantum Memory Palace "
             "Habits.\nThis topic leverages...")
    assert parse_topic(reply) == "Quantum Memory Palace Habits"


def test_parse_topic_from_narrative_uses_first_short_line():
    reply = "Your audience loves quick reflex puzzles.\nPublish a 3-second "
    "countdown timer challenge next.\nThis builds repeat loops."
    got = parse_topic(reply)
    assert got and got != reply


def test_parse_topic_empty():
    assert parse_topic("") == ""
    assert parse_topic("   ") == ""


def test_parse_topic_truncates_very_long():
    assert len(parse_topic("\n" + "x" * 500 + "\n")) <= 90


def test_is_refusal():
    assert _is_refusal("Sorry, I can't help with general content ideas.")
    assert _is_refusal("I don't have access to recommend that.")
    assert not _is_refusal(REAL_IDEATED_REPLY)
    assert not _is_refusal("Here is the topic: Numbers Challenge")
