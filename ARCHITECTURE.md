# Automation System — Architecture Proposal

## Objective
Take a single seed topic as input and autonomously produce and publish a faceless
short-form video (YouTube Shorts / Reels / TikTok) by driving **web interfaces
entirely through a persistent browser**, with zero human intervention bridging
steps and zero paid API / direct REST / token-billing endpoints.

## Constraint — "everything happens in the browser"
Every *external* workflow step (AI scripting, AI image generation, TTS, social
upload) is executed by driving a real, persistent browser context. The Python
process never calls any third-party HTTP API. Persistent profiles hold cookies /
auth so repeated logins are never required.

## Environment verified
- **Minimum:** Python 3.10+ (matches README). CI runs 3.12/3.13 on Linux.
- **Verified locally:** Windows, PowerShell 7, Python 3.14, FFmpeg 8.0.1 (local assembly).
- Playwright Python 1.58 installed. Microsoft Edge present (used as the persistent browser via Playwright's channel="msedge").

## Pipeline (sequential workflow manifest)
Each step's output is the exact input contract of the next. Steps never require a
human to relay data between browser sessions.

| # | Stage | Tool (browser-driven) | Output → next step |
|---|-------|-----------------------|--------------------|
| 1 | **Scripting** (LLM) | Free LLM web UI via persistent profile (Google AI Studio / DeepSeek web) — driven by Playwright | Script JSON {title, captions[], spoken_script} |
| 2 | **Asset generation** | Perchance AI image generator (free, no signup) via Playwright | N images (PNG/JPG), 9:16 |
| 3 | **Voiceover** | SoundTools/Kokoro in-browser TTS (WASM, no signup) via Playwright | audio.wav + duration (for caption timing) |
| 4 | **Assembly** | Local FFmpeg (no browser needed — it is local compute) | final.mp4 (1080x1920, burned captions, audio bed) |
| 5 | **Publishing** | YouTube Studio web upload via persistent context (TikTok / Instagram as future adapters) | live post URLs |

> Rationale: stage 4 assembly is pure local computation (FFmpeg + Pillow) and
> thereby *not* an "external task", so it is exempt from the browser rule.

## Core architecture
```
seed topic
   └─> orchestrator  (CLI entry: `python -m automato run "topic"`)
        |
        ├─ Workflow manifest (JSON): ordered stages + contracts + retry policy
        ├─ SessionManager  : per-provider persistent browser profiles
        │                    (profiles/<provider>/), edge channel, cookies persisted
        ├─ BrowserFactory  : Playwright launch_persistent_context, headless or headed
        ├─ adapters/       : one module per provider (bare `run(ctx, inputs, run_dir, session)` function)
        │     scripting/generic_llm.py
        │     assets/perchance_images.py
        │     tts/kokoro_tts.py
        │     publish/youtube_studio.py
        └─ resilience/     : retry, rate_limit, modal, location, interaction, recovery
```

### Key modules

1. **orchestrator.py** — reads the workflow manifest, runs stages sequentially,
   passes artifacts through typed dict handoffs, records state in a `run_state.json`
   ledger, and can resume from an aborted stage.

2. **SessionManager (browser/session.py + browser/factory.py)** — owns per-platform
   persistent directories (`profiles/youtube`, `profiles/ai_studio`,
   `profiles/perchance`, ...). First run opens a visible browser for one-time login
   (only the LLM/social sites that need it); subsequent runs reuse the persisted
   profile (cookies/localStorage). Implements a **pre-run auth check** (navigate to
   a logged-in-only URL, look for a stable ARIA element; if absent → surface a
   specific "re-auth required" signal).

3. **adapters/publish/youtube_studio.py** — the proven, published-in-wild flow:
   - open `https://studio.youtube.com`, click Create (ARIA `Create`), choose
     `Upload videos`, `set_input_files` on the hidden `input[type=file]`,
   - wait for the upload dialog `ytcp-uploads-dialog`,
   - fill title/description (contenteditable `#textbox`), set "Not for kids",
   - advance with `#next-button` through Details→Elements→Checks,
   - set visibility (public/unlisted/private) via paper-radio-button `name`,
   - click Publish, extract the final URL from the success dialog.
   Selectors use **semantic + ARIA locators**, never brittle full XPaths.

4. **resilience/** — `retry.py` (exponential backoff + jitter wrapper around
   every ambient element action), `rate_limit.py` (a **RateLimitAwareWaiter** that
   detects HTTP 429 / "too many requests" / challenge screens and waits rather than
   failing), `modal.py` (a **ModalDismisser** that sweeps for transient overlays,
   cookie banners, "stay logged in?", and interstitial dialogs before acting),
   `location.py` (centralized semantic/ARIA locator strings that re-resolve on
   retry, surviving UI drift), and `interaction.py` (element helpers that inject
   retry + modal-dismiss + backoff and prefer aria-label/role + text locators).

5. **resilience/recovery.py** — self-healing locator recovery: when the static
   locators fail for an interaction, it asks the LLM to synthesize candidate
   selectors, clicks the best match, and **learns** stable ones into a per-provider
   `profiles/<provider>/learned.json` overlay tried before the maintained static
   list.

## Workflow manifest (`workflows/faceless_short.json`)
```json
{
  "stages": [
    {"id": "script",   "adapter": "scripting.generic_llm",   "inputs": {"topic": "{{seed}}"},      "outputs": "script.json"},
    {"id": "assets",   "adapter": "assets.perchance_images", "inputs": {"script": "script.json", "count": 6}, "outputs": "assets/"},
    {"id": "voiceover","adapter": "tts.kokoro_tts",           "inputs": {"script": "script.json"}, "outputs": "voiceover.wav"},
    {"id": "assemble", "adapter": "assembly.ffmpeg" /* local */, "inputs": {"assets": "assets/", "voiceover": "voiceover.wav"}, "outputs": "final.mp4"},
    {"id": "publish",  "adapter": "publish.youtube_studio",   "inputs": {"video": "final.mp4"},    "outputs": "post_url.json"}
  ]
}
```
`{{seed}}` is the single user-provided topic.

## Resilience criteria mapped
- **End-to-end success**: sequential manifest + typed handoffs + local `state.json`
  resume; a crash in any stage is graceful and resumable from that stage.
- **Resilience**: RetryPolicy + RateLimitAwareWaiter (429) + ModalDismisser
  (interstitials) + generous `wait_for` on uploads/dialogs.
- **Element targeting**: semantic/ARIA locators in a single `Location` module;
  no brittle absolute XPaths; auto re-resolve + fallback locators.

## Directory layout
```
automation system/
  ARCHITECTURE.md
  README.md
  requirements.txt
  workflows/faceless_short.json
  automato/            # package
    __main__.py
    orchestrator.py
    manifest.py         # manifest loader + contract validation
    state.py            # state persistence / resume
    browser/
      factory.py        # persistent context factories (headless/headed, channel=msedge)
      session.py        # per-provider profiles + auth check
    adapters/
      base.py           # adapter contract: run(ctx, inputs) -> outputs + ExecutorError
      scripting/generic_llm.py
      assets/perchance_images.py
      tts/kokoro_tts.py
      assembly/ffmpeg.py   # local
      publish/youtube_studio.py
    resilience/
      retry.py
      rate_limit.py
      modal.py
      location.py
      interaction.py
      recovery.py       # LLM-assisted locator self-healing
    llm/                # generic prompt templates for script generation
      script_prompts.py
  profiles/             # persistent browser profiles per provider (created at runtime)
  output/               # artifacts per run
  workflows/            # workflow manifests
  tests/                # pytest suite (pure-logic + local ffmpeg stage) + CI
```

## Decisions I need from you
1. **Primary publish target**: I recommend **YouTube Shorts** first (most stable /
   proven automation path with session-only auth). TikTok & Instagram can be added
   as secondary adapters but are more finicky (strong sign-in / CAPTCHA). Confirm
   scope.
2. **LLM provider** for scripting: **Google AI Studio** (very reliable, free,
   optional login) vs **DeepSeek web** vs another. I recommend AI Studio.
3. **Browser mode**: default **headed** (visible) for first-run onboarding + safer
   against anti-bot; switch to **headless** for scheduled unattended runs. Confirm.
4. **Visibility** for the published video on the first dry run: `private`/`unlisted`
   for review, or `public` immediately?

I will not start writing code until you approve the architecture and answer these
four choices.
