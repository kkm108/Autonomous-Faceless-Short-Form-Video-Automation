"""Central configuration for the automation system.

Paths are resolved relative to this file's parent directory so the package works
from anywhere.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

PROFILES_DIR = ROOT / "profiles"
OUTPUT_DIR = ROOT / "output"
WORKFLOWS_DIR = ROOT / "workflows"
STATE_FILE = OUTPUT_DIR / "state.json"
ASSETS_DIR = ROOT / "assets"

# Channel registry (R8-A1): the structured data file that replaced the inline
# CHANNEL_WHITELIST/CHANNEL_MAP dicts. Add a channel by editing the YAML, not
# this module. Override the file location with AUTOMATO_CHANNELS_FILE.
CHANNELS_FILE = Path(
    os.environ.get("AUTOMATO_CHANNELS_FILE", "").strip() or (ROOT / "channels.yaml")
)


def _load_channel_registry() -> dict:
    """Parse the channel registry file (R8-A1).

    Returns ``{"default_channel": str, "channels": {name: profile}}``. An
    unreadable or missing registry is a hard misconfiguration: the engine's
    whole multi-channel routing (publish channel, scripting language, TTS voice,
    branding) derives from this one file, so fail loudly rather than silently
    defaulting to a one-channel world.
    """
    if not CHANNELS_FILE.is_file():
        raise FileNotFoundError(
            f"Channel registry not found: {CHANNELS_FILE}. R8 migrated the "
            "channel allowlist into channels.yaml; restore it or point "
            "AUTOMATO_CHANNELS_FILE at a valid registry."
        )
    try:
        data = yaml.safe_load(CHANNELS_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Channel registry {CHANNELS_FILE} is not valid YAML: {exc}")
    channels = data.get("channels") or {}
    if not isinstance(channels, dict) or not channels:
        raise ValueError(
            f"Channel registry {CHANNELS_FILE} must declare a non-empty "
            "'channels' mapping."
        )
    for name, profile in channels.items():
        cid = (profile or {}).get("channel_id") or ""
        if not cid or len(cid) != 24:
            raise ValueError(
                f"Channel '{name}' in {CHANNELS_FILE} has an invalid channel_id "
                f"{cid!r}; a channel id is exactly 24 alphanumerics/_-."
            )
    return {
        "default_channel": data.get("default_channel") or "main",
        "channels": channels,
    }

# Browser settings
BROWSER_CHANNEL = "msedge"          # used only when browser = edge/chrome channel launch
BROWSER_CHOICE = "edge"             # edge | chrome | brave | chromium


def _discover_browser_executables() -> dict:
    """Per-platform native binaries for channel-less launches (Brave, etc.).

    The primary override is the AUTOMATO_BRAVE_PATH env var; otherwise the OS's
    usual install locations are probed (R2-W1: previously a hardcoded Windows-only
    path, which made the 'brave' option unusable off Windows).
    """
    exes = {
        "brave": os.environ.get("AUTOMATO_BRAVE_PATH"),
        "chrome": None,
        "edge": None,
        "chromium": None,
    }
    if sys.platform == "win32":
        candidates = [
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        ]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]
    else:
        candidates = ["/usr/bin/brave-browser", "/usr/bin/brave", "/snap/bin/brave"]
    for cand in candidates:
        p = Path(os.path.expandvars(cand))
        if p.is_file() and exes["brave"] is None:
            exes["brave"] = str(p)
            break
    return exes


# Native executable paths for channel-less launches (Brave, hard Chrome installs).
BROWSER_EXECUTABLE = _discover_browser_executables()
# headless mode: "headed" | "new" (headless=new, looks like a real browser) | "full"
HEADLESS_MODE = "headed"
# Whether to inject anti-automation flags (recommended; helps avoid bot detection).
ANTI_AUTOMATION = True
HEADLESS = False                     # deprecated alias kept for compatibility
VIEWPORT = {"width": 1440, "height": 900}

# Stage-level retry (R1-W3): how many times a stage that raised
# ExecutorError(retryable=True) is re-attempted before the run fails.
STAGE_RETRY_ATTEMPTS = 2

# Whole-run process guard (R2-W3/R2-F3): a run whose lock heartbeat is older than
# RUN_LOCK_STALE_S is considered dead and its lock is reclaimed, so a crashed run
# can never permanently wedge scheduling; a younger lock refuses a second run.
# Should comfortably exceed the longest single stage (incl. retries).
RUN_LOCK_STALE_S = 3600

# Quality gates (R2-W4/R2-F4): when a soft-fail gate trips on any stage, publish
# is forcibly downgraded to "unlisted" even if the run explicitly asked for public.
FORCE_UNLISTED = False

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

# Modal dismissal (R3-W4): sweep for interstitials before each action unless the
# caller opts out; provider-scoped lists run before the generic baseline. Override
# with AUTOMATO_MODAL_DISMISS_ENABLED=0|false|no.
_MODAL_ENV = os.environ.get("AUTOMATO_MODAL_DISMISS_ENABLED", "").strip().lower()
MODAL_DISMISS_ENABLED = True if not _MODAL_ENV else _MODAL_ENV not in (
    "0", "false", "no", "off")

# Learned-selector lifecycle (R3-W2/R3-F2): learned overlay entries are stamped
# with when/why they were learned, expire after LEARNED_SELECTOR_TTL_DAYS, and are
# also dropped after LEARNED_STATIC_HITS_TO_EXPIRE consecutive runs where the
# maintained static selector resolved the group successfully.
LEARNED_SELECTOR_TTL_DAYS = 30
LEARNED_STATIC_HITS_TO_EXPIRE = 20

# Adapter resolution policy (R3-W5/R3-F3): only in-tree automato.adapters.* paths
# are importable from workflow JSON by default. Fully-qualified *external*
# modules require an explicit opt-in, via the AUTOMATO_ALLOW_EXTERNAL_ADAPTERS env
# var or by editing ALLOW_EXTERNAL_ADAPTERS.
ALLOW_EXTERNAL_ADAPTERS = (
    os.environ.get("AUTOMATO_ALLOW_EXTERNAL_ADAPTERS", "").strip().lower()
    in ("1", "true", "yes")
)


# Pipeline
DEFAULT_WORKFLOW = "faceless_short"
DEFAULT_VISIBILITY = "unlisted"      # unlisted, private, public

# Scripting LLM provider. "gemini" (web guest, no login) is the default because
# Google gated Gemini 3 Flash Preview in the free AI Studio Playground behind a
# Google AI Plan / API key. Pin AUTOMATO_LLM_PROVIDER=ai_studio to use a
# logged-in AI Studio with a paid plan; otherwise the adapter tries the
# preferred provider first, then falls back through the no-login chain.
LLM_PROVIDER = "gemini"

# No-login LLM fallback chain (R6): tried in order after the user's preferred
# provider. All are login-free web chat UIs: duck.ai, Ask Brave, Gemini (guest,
# base Flash), and ChatGPT (guest, region-gated, last resort). Runtime failures
# fall through to the next entry. Override with
# AUTOMATO_LLM_NO_LOGIN_CHAIN="duckai,geminiai,..."
_LLM_CHAIN_ENV = os.environ.get("AUTOMATO_LLM_NO_LOGIN_CHAIN", "").strip()
LLM_NO_LOGIN_CHAIN = (
    [p.strip() for p in _LLM_CHAIN_ENV.split(",") if p.strip()]
    if _LLM_CHAIN_ENV
    else ["duckai", "ask_brave", "gemini", "chatgpt"]
)

# TTS strategy. "auto" tries the browser web tool (SoundTools) then edge-tts then
# pyttsx3 on failure. You can force one: "soundtools" | "edge_tts" | "pyttsx3".
TTS_PROVIDER = "auto"

# Language-aware routing (R6): classical-verse/chant Sanskrit (`sa`) may route to
# the online vagdhenu (IISc) engine, but that adapter is opt-in and not yet
# wired; enabling only slots it ahead of the local chain. General speech always
# stays on the local chain. Override with AUTOMATO_VAGDHENU_ENABLED=1.
VAGDHENU_ENABLED = (
    os.environ.get("AUTOMATO_VAGDHENU_ENABLED", "").strip().lower()
    in ("1", "true", "yes")
)
# Bounded wait for the browser TTS tool before falling back (seconds).
TTS_BROWSER_TIMEOUT_S = 120
# edge-tts voice (high-quality neural voice used by Microsoft Edge read-aloud).
EDGE_TTS_VOICE = "en-US-ChristopherNeural"
# R8-A2: language -> Edge neural voice. The channel registry's `language` field
# (single source of truth) selects the voice; unknown languages fall back to
# EDGE_TTS_VOICE.
EDGE_TTS_VOICES = {
    "en": EDGE_TTS_VOICE,
    "es": "es-ES-AlvaroNeural",
    "hi": "hi-IN-MadhurNeural",
}
# Languages the registry may route to (publish channel + script + TTS voice).
KNOWN_LANGUAGES = ("en", "es", "hi", "sa")

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
# R6 batch controls: the generator's native Shape/"How many" selects. Shape
# "512x768" = Portrait (native 2:3, ideal for vertical videos) and numImages
# batches that many variants of a single prompt in parallel. Set shape to "" or
# numImages to 0 to leave the site defaults alone.
PERCHANCE_SHAPE = os.environ.get("AUTOMATO_PERCHANCE_SHAPE", "512x768")
PERCHANCE_NUM_IMAGES = int(os.environ.get("AUTOMATO_PERCHANCE_NUM_IMAGES", "4") or 0)
# youtube_studio
YOUTUBE_UI_SETTLE_S = 3               # post-navigation settle
YOUTUBE_POST_CLICK_SLEEP_S = 2        # micro-settle between workflow steps
YOUTUBE_UPLOAD_DEADLINE_S = 120       # deadline for the upload dialog to reach DETAILS
YOUTUBE_POST_PUBLISH_SLEEP_S = 6      # settle after clicking Publish/Done
# A just-published short stays "processing" for a couple of minutes: it isn't in
# the Studio Videos list until then, so URL capture waits up to this long for the
# row to appear (R6 publish-stale-URL surveillance finding).
YOUTUBE_PUBLISH_LISTING_WAIT_S = 420
# The final Publish/Done button stays aria-disabled until the just-uploaded
# file finishes processing (Shorts transcoding can take a few minutes); jumping
# straight to the click exhausts Playwright's click timeout against the disabled
# button + its overlay backdrop (E2E double observation on the R7 publishes).
# Wait for it to enable before clicking.
YOUTUBE_PUBLISH_READY_WAIT_S = 300


# ---- Brand-channel routing (R7/R8) ----
# R8-A1: the allowlist, topic map and per-channel profiles now all derive from
# the external channel registry (channels.yaml), keeping the resolution logic in
# channels.py unchanged in *shape* while the data lives in one hand-editable
# file. The AUTOMATO_CHANNEL_WHITELIST / AUTOMATO_CHANNEL_MAP environment
# overrides (JSON, re-read per call in channels.py) still win for the two legacy
# shapes when set.
_CHANNEL_REGISTRY = _load_channel_registry()
CHANNEL_DEFAULT = _CHANNEL_REGISTRY["default_channel"]
# name -> full registry entry (channel_id, language, keywords, branding).
CHANNEL_PROFILES = dict(_CHANNEL_REGISTRY["channels"])
# {name: 24-char channel id} (same shape as the pre-R8 dict; env override wins).
_CHANNEL_WHITELIST_ENV = os.environ.get("AUTOMATO_CHANNEL_WHITELIST", "").strip()
CHANNEL_WHITELIST = (
    json.loads(_CHANNEL_WHITELIST_ENV)
    if _CHANNEL_WHITELIST_ENV
    else {name: (profile.get("channel_id") or "")
          for name, profile in CHANNEL_PROFILES.items()}
)
# topic-keyword -> channel name; first keyword hit wins (case-insensitive),
# scanned in registry order (the deterministic R8-A4 tie-break). Used to route
# a seeded or ideated topic to the right brand channel. Override with
# AUTOMATO_CHANNEL_MAP (JSON object mapping channel names to keyword lists).
_CHANNEL_MAP_ENV = os.environ.get("AUTOMATO_CHANNEL_MAP", "").strip()
CHANNEL_MAP = (
    json.loads(_CHANNEL_MAP_ENV)
    if _CHANNEL_MAP_ENV
    else {name: tuple(profile.get("keywords") or ())
          for name, profile in CHANNEL_PROFILES.items()}
)
# confirm-before-publish: the publish stage asks the operator for an explicit
# "y" immediately before clicking Publish, and ABORTS on anything else. This is
# the shipping default because a wrong-channel publish is a visible mistake.
# Disable (or pre-confirm) with AUTOMATO_PUBLISH_CONFIRM=0|false or the CLI
# --yes flag.
PUBLISH_CONFIRM = True

# Topic-ideation pre-stage (Ask Studio): a run without an explicit seed topic
# derives one from the active channel's Ask Studio assistant, which reasons over
# the channel's OWN performance data. Ask Studio refuses general content
# requests, so the question must stay frameworked around the channel's metrics.
# Disable with AUTOMATO_TOPIC_IDEATION_ENABLED=0|false; force with the CLI
# --ideate flag.
TOPIC_IDEATION_ENABLED = True
# Channel-scoped question asked to Ask Studio. Override with
# AUTOMATO_ASK_STUDIO_QUESTION.
ASK_STUDIO_QUESTION = (
    "Based on my channel's recent shorts performance and audience behavior, what "
    "single topic should my next short cover to maximize next-stage traffic? "
    "Recommend one concrete topic."
)
# Hard deadline for an Ask Studio answer (it streams in tens of seconds).
ASK_STUDIO_REPLY_WAIT_S = 240
# How long to wait for the chat composer to appear after opening the drawer.
ASK_STUDIO_COMPOSER_WAIT_S = 30


# ---- R8-B3/B4: background music + SFX ----
# Drop properly-licensed (CC-BY/CC0, clearly documented) background tracks into
# ASSETS_DIR/music; the assembly stage ducks them under the voiceover. Disable
# with AUTOMATO_MUSIC_ENABLED=0.
_MUSIC_ENV = os.environ.get("AUTOMATO_MUSIC_ENABLED", "").strip().lower()
MUSIC_ENABLED = True if not _MUSIC_ENV else _MUSIC_ENV not in ("0", "false", "no",
                                                            "off")
MUSIC_DIR = ASSETS_DIR / "music"
# Optional per-slide sound effects: assets/sfx/sfx_<i>.(mp3|wav) keyed to slide
# transitions; mixed at the start of each slide's window when present.
SFX_DIR = ASSETS_DIR / "sfx"
# Ducking levels for the narration-driven sidechain compressor (R8-B3).
MUSIC_DUCK_ATTACK_MS = 20
MUSIC_DUCK_RELEASE_MS = 300
MUSIC_DUCK_RATIO = 8
MUSIC_BED_LEVEL = 0.35          # post-duck music volume (0..1)

# ---- R8-B7: custom thumbnail ----
# Produce a channel-branded thumbnail, and OPTIONALLY upload it in the publish
# stage. The upload interaction is new Studio automation and is opt-in until it
# has been live-validated; the thumbnail artifact is always built. Enable with
# AUTOMATO_UPLOAD_THUMBNAIL=1.
YOUTUBE_UPLOAD_THUMBNAIL = (
    os.environ.get("AUTOMATO_UPLOAD_THUMBNAIL", "").strip().lower()
    in ("1", "true", "yes")
)


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
