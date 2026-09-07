# Autonomous Faceless Short-Form Video Automation

Turns a single seed topic into a finished, **published** short-form video
(YouTube Shorts) using **only browser automation** for every external step —
no paid APIs, no direct REST integrations, no token billing.

## How it satisfies the constraints

| Constraint | Implementation |
|---|---|
| External tasks via browser only | AI scripting (Google AI Studio), image gen (Perchance), TTS (Kokoro in-browser), and YouTube upload are all driven through a real Edge session via Playwright. |
| No paid API / REST / tokens | No provider HTTP API is ever called by the Python code. It scripts the web UIs themselves. Video assembly is local FFmpeg + Pillow (local compute, not "external"). |
| Persistent sessions, no repeated logins | Each provider keeps its own Edge profile under `profiles/<name>/` (cookies + localStorage survive across runs). One-time `login` prepares accounts. |

## Architecture

```
seed topic
  └─ orchestrator (sequential workflow manifest, typed handoffs, crash-resume)
      ├─ scripting  → generic_llm      (Google AI Studio via browser)   → script.json
      ├─ assets     → perchance_images (Perchance, free, no signup)     → assets/*.jpg
      ├─ voiceover  → kokoro_tts       (SoundTools in-browser Kokoro)    → voiceover.wav
      ├─ assemble   → ffmpeg           (local FFmpeg + Pillow)           → final.mp4
      └─ publish    → youtube_studio   (YouTube Studio web upload)       → post_url.json
```

Resilience is bundled in `automato/resilience/`:
- **RetryPolicy** — exponential backoff + jitter around every ambient action.
- **RateLimitAwareWaiter** — detects HTTP 429 / challenge / "slow down" and waits.
- **ModalDismisser** — sweeps cookie banners, "stay signed in?", tour overlays, etc.
- **Location** — centralized semantic/ARIA locators with fallbacks (no brittle XPaths).

## Prerequisites

- Python 3.10+ with a browser. On Windows/macOS the default `--browser edge`
  reuses your installed Edge; use `--browser chromium` for the Playwright-bundled
  browser everywhere else (run `playwright install chromium` once for that). A
  bundled open-license fallback font makes the local assembly stage work on any OS.
- FFmpeg + ffprobe on `PATH` (used only for local assembly):
  - Windows: `winget install ffmpeg` · Linux: `sudo apt install ffmpeg` · macOS: `brew install ffmpeg`
- Python packages:
  ```powershell
  pip install -r requirements.txt
  ```
  ```bash
  pip install -r requirements.txt
  ```

## Usage

### 1. One-time login (only the providers that need an account)

YouTube needs a signed-in session (no cookies are stored in the repo — only a
local profile). This opens a *visible* browser; sign in, then close it:

```powershell
python -m automato login youtube     # sign into your Google/YouTube Studio account
python -m automato login ai_studio   # optional: sign into Google AI Studio
# perchance and tts require no login
```

```bash
python -m automato login youtube
python -m automato login ai_studio
```

After this, sessions persist in `profiles/` — no repeat logins during runs.

### 2. Run the full pipeline

```powershell
python -m automato run "the science of sleep" --visibility unlisted
```

```bash
python -m automato run "the science of sleep" --visibility unlisted
```

Options:
- `--visibility {public,unlisted,private}` (default `unlisted`).
- `--headless` to hide browsers (best for unattended scheduled runs).
- `--browser {edge,chrome,brave,chromium}` (default `edge`; requires
  `playwright install chromium` for the bundled `chromium` build).
- `-v` for verbose logs.

Each run writes artifacts under `output/<run_id>/` (`script.json`, `assets/`,
`voiceover.wav`, `final.mp4`, `post_url.json`) plus a `run_state.json` ledger so a
crashed run can be resumed at the first incomplete stage.

### 3. Scheduling unattended runs (safely)

For cron / Task Scheduler / CI-driven runs:

- **A whole-run lock (`output/run.lock`) refuses to run two workflows at once.**
  A second run started while the first is active fails fast instead of sharing
  (and corrupting) the same Chromium profiles. If a run crashes, its lock goes
  stale (`RUN_LOCK_STALE_S`, default 3600s) and is reclaimed automatically — a
  dead process can never permanently wedge scheduling, but a live one can never
  be silently bypassed.
- **Minimum scheduling interval:** at least **2× the longest observed run**,
  and in practice 2–3 hours. A single run already does 5 browser stages with
  per-stage deadlines (up to ~200 s each) plus retries; a run that starts before
  the previous one has finished can only rot on the lock until it goes stale.
- Run the schedulable command headless
  (`--headless` / `--headless-mode new`) so a stuck challenge fails fast
  (`ChallengeError`) instead of waiting invisibly for a human.
- Give every scheduled invocation its own log file (`-v >> runs.log 2>&1`) — the
  exit code is 0 on success, 1 on an expected/handled failure, and 130 on
  interruption, so a scheduler can react per code.
- Run **backups on the same cadence** (`python -m automato backup`); archives are
  AES-encrypted by default and contain live session data.

## Automated dry-run of the local stage

The assembly stage is fully local and can be smoke-tested without any account or
a live third-party site.

## Adding providers / adapters

- Add an adapter under `automato/adapters/<category>/<name>.py` exposing
  `run(ctx, inputs, run_dir, session=None) -> dict`.
- Declare its provider profile mapping in `automato/orchestrator.py` (`_ADAPTER_PROVIDER`)
  and register an optional auth check in `automato/providers.py`.
- Wire it into the ordered stages of `workflows/faceless_short.json`.

## Notes & limitations

- YouTube Studio's DOM changes over time; the locators in
  `automato/adapters/publish/youtube_studio.py` use semantic ARIA + text fallbacks
  and are re-resolved on retry to survive minor drift. If YouTube changes a
  selector, update the `LOCS` table.
- Live browser stages (AI Studio, Perchance, SoundTools) depend on those third-party
  UIs being reachable; the resilience layer retries and rate-limit-waits, and the
  run resumes rather than crashing.
- Perchance sits behind Cloudflare; this is handled by using the real persistent
  Edge profile (looks like a genuine user session) rather than a bare request.
- **Content-safety / platform-policy stance.** This engine is *generic* automation
  tooling — it scripts the same UI a human would use. It is not a warrant of
  compliance with the platforms you publish on, and the realistic policies are
  worth naming for anyone who opts into `--visibility public` or a real
  schedule:
  - **YouTube monetization (Repetitious Content policy):** mass-produced,
    reused, or templated/repetitive content is not eligible for monetization,
    and repeatedly-published near-identical videos can be restricted or removed
    regardless of monetization. A scheduled run of the *same* workflow with a
    canned topic string is exactly the kind of behavior this policy targets.
  - **AI-generated content disclosure:** YouTube (and other platforms) require
    disclosing altered/synthetic media — AI-generated narration, imagery, or
    deepfake-looking material — for videos; undisclosed synthetic content can be
    flagged and demonetized. This pipeline's script, images, and TTS are all
    AI-generated, so disclosure belongs in the video description/title metadata.
  - **Comparative/mass-account abuse:** thousands of identical accounts posting
    on a cadence triggers automated anti-abuse systems. Spread accounts,
    vary content, and treat publishing as a human responsibility.
  The project's safety instincts already encode the right defaults: `unlisted` is
  the default visibility (nothing goes public unless you say so), and CAPTCHA /
  login challenges are never auto-solved (`challenge.py` fails fast instead).
  Keep those instincts: publish widely, iterate on real content, and disclose
  synthetic media.
"# Autonomous-Faceless-Short-Form-Video-Automation" 
