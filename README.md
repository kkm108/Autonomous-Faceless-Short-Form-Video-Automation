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

- Python 3.10+ on Windows with Microsoft Edge installed.
- FFmpeg + ffprobe on `PATH` (used only for local assembly).
- Playwright Python and Pillow:

```powershell
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

After this, sessions persist in `profiles/` — no repeat logins during runs.

### 2. Run the full pipeline

```powershell
python -m automato run "the science of sleep" --visibility unlisted
```

Options:
- `--visibility {public,unlisted,private}` (default `unlisted`).
- `--headless` to hide browsers (best for unattended scheduled runs).
- `-v` for verbose logs.

Each run writes artifacts under `output/<run_id>/` (`script.json`, `assets/`,
`voiceover.wav`, `final.mp4`, `post_url.json`) plus a `run_state.json` ledger so a
crashed run can be resumed at the first incomplete stage.

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
"# Autonomous-Faceless-Short-Form-Video-Automation" 
