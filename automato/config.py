"""Central configuration for the automation system.

Paths are resolved relative to this file's parent directory so the package works
from anywhere.
"""
from __future__ import annotations

import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROFILES_DIR = ROOT / "profiles"
OUTPUT_DIR = ROOT / "output"
WORKFLOWS_DIR = ROOT / "workflows"
STATE_FILE = OUTPUT_DIR / "state.json"

# Browser settings
BROWSER_CHANNEL = "msedge"          # used only when browser = edge/chrome channel launch
BROWSER_CHOICE = "edge"             # edge | chrome | brave | chromium
# Native executable paths for channel-less launches (Brave, hard Chrome installs).
BROWSER_EXECUTABLE = {
    "brave": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    "chrome": None,
    "edge": None,
    "chromium": None,
}
# headless mode: "headed" | "new" (headless=new, looks like a real browser) | "full"
HEADLESS_MODE = "headed"
# Whether to inject anti-automation flags (recommended; helps avoid bot detection).
ANTI_AUTOMATION = True
HEADLESS = False                     # deprecated alias kept for compatibility
VIEWPORT = {"width": 1440, "height": 900}

# Stage-level retry (R1-W3): how many times a stage that raised
# ExecutorError(retryable=True) is re-attempted before the run fails.
STAGE_RETRY_ATTEMPTS = 2

# Resilience defaults
DEFAULT_TIMEOUT_MS = 30000
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY_S = 2.0
RETRY_BACKOFF = 2.0
RETRY_JITTER_S = 0.5
RATE_LIMIT_MIN_WAIT_S = 10.0
RATE_LIMIT_MAX_WAIT_S = 90.0

# LLM recovery agent ("learn like a human" on failures)
RECOVERY_ENABLED = True
RECOVERY_DEADLINE_S = 200

# Challenge (CAPTCHA / 2FA / unexpected login) handling
CHALLENGE_PAUSE_SECONDS = 180
HUMAN_DONE_FLAG = "output/.human_done"
CHALLENGE_CHECK = True


# Pipeline
DEFAULT_WORKFLOW = "faceless_short"
DEFAULT_VISIBILITY = "unlisted"      # unlisted, private, public

# Scripting LLM provider ("ai_studio" is the user's preferred; the adapter falls
# back to "duckai", which needs no login, when AI Studio isn't signed in).
LLM_PROVIDER = "ai_studio"

# TTS strategy. "auto" tries the browser web tool (SoundTools) then edge-tts then
# pyttsx3 on failure. You can force one: "soundtools" | "edge_tts" | "pyttsx3".
TTS_PROVIDER = "auto"
# Bounded wait for the browser TTS tool before falling back (seconds).
TTS_BROWSER_TIMEOUT_S = 120
# edge-tts voice (high-quality neural voice used by Microsoft Edge read-aloud).
EDGE_TTS_VOICE = "en-US-ChristopherNeural"

# Local ffmpeg/ffprobe budget (R1-W7): a stuck encode must not hang the pipeline
# forever; this is the iteration timeout for each subprocess call.
FFMPEG_TIMEOUT_S = 600

# R1-F4: adapter timing constants — one place to tune the whole engine.
# generic_llm (AI Studio / duck.ai scripting)
GENERIC_LLM_INIT_TIMEOUT_MS = 12000   # time to find the prompt compose box
GENERIC_LLM_POLL_DEADLINE_S = 200     # hard deadline waiting for the model reply
# perchance_images
PERCHANCE_SETTLE_S = 12               # generator iframe/UI settle after navigating
PERCHANCE_UI_SETTLE_S = 3             # settle between saved generations
PERCHANCE_MAX_PER_IMAGE_S = 150       # per-generation hard deadline
PERCHANCE_POLL_INTERVAL_S = 5
# youtube_studio
YOUTUBE_UI_SETTLE_S = 3               # post-navigation settle
YOUTUBE_POST_CLICK_SLEEP_S = 2        # micro-settle between workflow steps
YOUTUBE_UPLOAD_DEADLINE_S = 120       # deadline for the upload dialog to reach DETAILS
YOUTUBE_POST_PUBLISH_SLEEP_S = 6      # settle after clicking Publish/Done


def ensure_dirs() -> None:
    for d in (PROFILES_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


# Which third-party package each TTS method needs to be importable.
TTS_DEPENDENCIES = {
    "soundtools": (),  # browser-driving only; needs a session, not a package
    "edge_tts": ("edge_tts",),
    "pyttsx3": ("pyttsx3",),
    "auto": ("edge_tts", "pyttsx3"),
}


def validate_environment(provider: str = "auto") -> list:
    """Check that packages needed for the *configured* TTS provider are importable.

    Returns a list of missing dependency names (empty = all present). This is a
    startup fail-fast for the documented "guaranteed" fallback chain: if a fallback
    link is missing, the run fails here instead of deep inside an adapter.
    """
    missing = []
    for mod in TTS_DEPENDENCIES.get(provider, TTS_DEPENDENCIES["auto"]):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    return missing
