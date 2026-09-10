"""R7: Ask Studio topic-ideation — topic parsing against real captured replies
and refusal detection."""

from automato.adapters.ideation.ask_studio import (
    _is_final_answer,
    _is_refusal,
    _strip_question_echo,
    parse_topic,
)

QUESTION = (
    "Based on my channel's recent shorts performance and audience behavior, "
    "what single topic should my next short cover to maximize next-stage "
    "traffic? Recommend one concrete topic."
)

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


def test_parse_topic_prefers_title_case_line_without_marker():
    # Live Captured (no "Recommended" marker this time): the concrete topic is
    # the Title-Case phrase, not the surrounding narrative sentences.
    reply = (
        "Looking at your recent Shorts performance for AI.Powered.Skills, "
        "bite-sized, interactive logic and math puzzles are driving your "
        "strongest engagement.\n"
        "High retention on quick number puzzles\n"
        "AI Math Riddle That 95 Percent of Viewers Solve Completely Wrong\n"
        "Interactive fast paced logic puzzles drive your channel's highest "
        "retention and viewer engagement\n"
        "Tell me more\n"
        "AI can make mistakes. You are responsible for the content you publish."
    )
    assert parse_topic(reply) == (
        "AI Math Riddle That 95 Percent of Viewers Solve Completely Wrong")


def test_parse_topic_marker_block_beats_longest_disclaimer():
    # Live capture: the 'Recommended Topic' block names the concrete title as a
    # Title-Case line, but the LONGEST candidate in that block was the card's
    # disclaimer footer. Title-case must win over length, and boilerplate must
    # never be returned.
    reply = (
        "Recommended Topic\n"
        "Based on top performing interactive challenge Shorts\n"
        "Interactive Pause Challenge Spot the Odd Number Hidden in Rapid Grid\n"
        "Fast interactive visual puzzles drive your channel's highest retention "
        "rates and spark viewer comment engagement\n"
        "Tell me more\n"
        "AI can make mistakes. You are responsible for the content you publish. "
        "Learn more"
    )
    assert parse_topic(reply) == (
        "Interactive Pause Challenge Spot the Odd Number Hidden in Rapid Grid")


def test_parse_topic_truncation_does_not_split_words():
    long = ("The " + "amazing " * 25 + "word")  # well over 90 chars
    got = parse_topic(long)
    assert len(got) <= 90
    assert not got.endswith(" ")


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


def test_strip_question_echo_removes_user_bubble():
    # Observed live: the captured region opens with the echoed question (wrapped
    # mid-word, "behavior," -> "havior,") plus stream status and the disclaimer.
    region = (
        "havior, what single topic should my next short cover to maximize "
        "next-stage traffic? Recommend one concrete topic.\n"
        "Gathering the comments across your channel...\n"
        "AI can make mistakes. You are responsible for the content you publish.\n"
        "Make it a bolt-of-lightning reflex puzzle.\n"
    )
    cleaned = _strip_question_echo(region, QUESTION)
    assert "what single topic" not in cleaned
    assert "next-stage traffic" not in cleaned
    assert "Make it a bolt-of-lightning reflex puzzle." in cleaned


def test_strip_question_echo_keeps_real_answer():
    cleaned = _strip_question_echo(REAL_IDEATED_REPLY, QUESTION)
    assert cleaned == REAL_IDEATED_REPLY
    assert "Recommended Concrete Topic" in cleaned and "Fast Paced" in cleaned


def test_final_answer_rejects_status_and_disclaimer_only():
    assert not _is_final_answer(
        "Gathering the comments across your channel...\nAI can make mistakes. "
        "You are responsible for the content you publish. Learn more"
    )
    assert _is_final_answer(
        "Gathering the comments across your channel...\nRecommended Concrete "
        "Topic.\nA 3-second countdown timer challenge."
    )
    assert not _is_final_answer("  \n  \n")
    assert not _is_final_answer("")


def test_parse_topic_skips_disclaimer_as_topic():
    # The fallback path must not pick the assistant-card disclaimer.
    reply = (
        "Gathering the comments across your channel...\n"
        "AI can make mistakes. You are responsible for the content you publish."
    )
    assert parse_topic(reply) == ""
    assert "AI can make mistakes" not in parse_topic(reply)
