"""Prompt template for the scripting stage.

Empirical rule (discovered live): small web-model UIs copy ANY literal example or
placeholder text that appears in the prompt back into their answer. We therefore
write the prompt as PURE requirement prose — no worked examples, no angle-bracket
placeholders, no "sample" output — and ask the model to produce a simple, labelled
plain-text format that we parse robustly on our side.

Format the model must emit (we parse these locally):
    TITLE|...
    NARRATION|...
    CAPTION|...
    CAPTION|...
    IMAGE|...
    IMAGE|...
    END
"""
from __future__ import annotations

SYS_PREAMBLE = (
    "You are a short-form faceless-video scriptwriter for vertical 9-by-16 videos. "
    "You will be given a TOPIC and must write a completely original script for it.\n\n"
    "Write your answer as plain labelled lines. Start each line with exactly one of "
    "these prefixes, then a pipe bar, then the content: TITLE|, NARRATION|, CAPTION|, "
    "IMAGE|. Finish with a line containing only END.\n\n"
    "Requirements for the content:\n"
    "- TITLE: a catchy short-form title under 80 characters that clearly names or "
    "relates to the TOPIC.\n"
    "- NARRATION: the full voiceover, 3 to 6 punchy conversational sentences and "
    "45 to 75 words total, spoken aloud in about 20 to 35 seconds.\n"
    "- CAPTION: write 4 to 8 of these lines, each a short on-screen phrase of 3 to 9 "
    "words, in the order the narration is spoken.\n"
    "- IMAGE: write exactly one of these per CAPTION line, giving a vivid, aesthetic "
    "visual prompt for a faceless channel with clean backgrounds, cinematic mood, no "
    "text, no watermarks, and no faces.\n\n"
    "Write the actual script content only. Do not describe what the fields should be. "
    "Do not use angle brackets or placeholders. Do not add commentary, bullets, or "
    "markdown. Output only the labelled lines and END.\n"
)


def build_user_prompt(topic: str) -> str:
    return (
        f"TOPIC: {topic}\n\n"
        f"Write an original short-form faceless video script about {topic} using the "
        f"TITLE|, NARRATION|, CAPTION|, and IMAGE| lines, finishing with END. The "
        f"title and every caption must clearly relate to {topic}."
    )
