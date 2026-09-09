# Autonomous Faceless Short-Form Video Automation

Turn a single seed topic into a **finished, published** short-form video (YouTube
Shorts) using **only browser automation** — no paid APIs, no direct REST
integrations, no token billing, no keys except the passwords to your own
accounts.

The full pipeline: an LLM writes the script → free image generator draws the
backgrounds → a neural TTS reads the narration → local FFmpeg assembles the MP4 →
YouTube Studio uploads it. Everything external is a real browser session with a
persistent profile, exactly like a person sitting at a desk.

- **Quick-start users:** [60-second setup](#quick-start-60-seconds)
- **Advanced users:** work your way down — [every command](#every-command-at-a-glance),
  [personalization](#personalization), [editing the engine](#editing-the-engine-advanced),
  [backups and portability](#backups--portability), [use cases](#use-cases),
  [FAQ](#faq), [troubleshooting](#troubleshooting).

---

## Quick start (60 seconds)

Assume a terminal already on this folder, Python 3.10+ and a browser installed.

```powershell
# 1. install (Windows)
pip install -r requirements.txt
winget install ffmpeg   # if ffprobe isn't already on PATH

# 2. protect the repo from committing live sessions (one-time)
git config core.hooksPath .githooks

# 3. sign into YouTube Studio (opens a visible browser; close it when done)
python -m automato login youtube

# 4. run the whole pipeline, unlisted (safe default, nothing public)
python -m automato run "the science of sleep"
```

```bash
# 1. install (macOS / Linux)
pip install -r requirements.txt
brew install ffmpeg     # or: sudo apt install ffmpeg

# 2. one-time pre-commit guard
git config core.hooksPath .githooks

# 3. sign into YouTube Studio
python -m automato login youtube

# 4. run it
python -m automato run "the science of sleep"
```

That's it. The first run takes a few minutes (it opens and closes five browser
sessions). When it finishes you'll see a URL in
`output/<run_id>/post_url.json`. The video is **unlisted** — nothing goes public
until you say so.

If the browser can't be found, add `--browser chromium` and run
`playwright install chromium` once (see [browser variants](#browser-variants)).

---

## What the pipeline does

| Stage | Adapter | Provider | Needs login? |
|---|---|---|---|
| Scripting | `scripting.generic_llm` | Gemini web guest → duck.ai → Ask Brave → ChatGPT (no-login); AI Studio optional | No |
| Assets | `assets.perchance_images` | Perchance free image generator | No |
| Voiceover | `tts.kokoro_tts` | SoundTools (browser) → edge-tts → pyttsx3 | No |
| Assembly | `assembly.ffmpeg` | Local FFmpeg + Pillow (no network) | — |
| Publish | `publish.youtube_studio` | YouTube Studio web upload | **Yes** |

## Constraint fit (why this is built this way)

| Constraint | Implementation |
|---|---|
| External tasks via browser only | AI scripting, image gen, TTS, and upload all drive a real Edge session via Playwright. No provider HTTP API is ever called in Python. |
| No paid API / REST / tokens | The code scripts the web UIs themselves. Assembly is local compute (FFmpeg + Pillow). |
| Persistent sessions, no repeat logins | Each provider keeps its own profile under `profiles/<name>/` (cookies + localStorage survive across runs). Sessions are never stored in git. |

## Architecture

```
seed topic
  └─ orchestrator (typed workflow manifest, crash-resume, whole-run lock)
      ├─ scripting  → generic_llm       → script.json
      ├─ assets     → perchance_images  → assets/*.jpg
      ├─ voiceover  → kokoro_tts        → voiceover.wav
      ├─ assemble   → ffmpeg            → final.mp4
      └─ publish    → youtube_studio    → post_url.json
```

Resilience lives in `automato/resilience/`:

- **RetryPolicy** — exponential backoff + jitter around every ambient action.
- **RateLimitAwareWaiter** — notices HTTP 429 / "slow down" and waits instead of hammering.
- **ModalDismisser** — sweeps cookie banners, "stay signed in?", tour overlays before each click. Opt out with `AUTOMATO_MODAL_DISMISS_ENABLED=0`.
- **Location** — semantic/ARIA locators with fallbacks; no brittle XPaths.
- **LLM recovery agent** — on failure it diagnoses and repairs/learns selectors, so the run resumes rather than crashes.

Deep-dive: [`ARCHITECTURE.md`](ARCHITECTURE.md) documents internals;
[`SECURITY.md`](SECURITY.md) documents the security model (sessions, encryption,
challenge handling).

---

## Every command at a glance

| Command | What it does |
|---|---|
| `python -m automato run "<topic>"` | Run the full pipeline. |
| `python -m automato login <provider>` | One-time visible login (`youtube`, `ai_studio`, `perchance`). |
| `python -m automato health-check` | Re-validate provider locators against live sites (monthly drift check). |
| `python -m automato trend` | Show LLM-recovery rates per provider (drift early-warning). |
| `python -m automato replay-import file.json` | Turn a DevTools Recorder export into locator hints. |
| `python -m automato backup` | AES-encrypted, portable snapshot of profiles/workflows/config/output. |
| `python -m automato restore archive.zip` | Restore a backup, rebinding paths to the new machine. |

Add `-v` to any command for verbose logs.

---

## `run` in depth

```
python -m automato run "<topic>" [options]
```

| Option | Values | Default | Meaning |
|---|---|---|---|
| `--visibility` | `public` \| `unlisted` \| `private` | `unlisted` (or `AUTOMATO_VISIBILITY`) | Where the upload lands. |
| `--workflow` | name | `faceless_short` | `workflows/<name>.json` manifest to run. |
| `--resume` | run id | — | Resume an interrupted run at its first incomplete stage. |
| `--headless` | flag | off | Full headless (best for schedules/CI). |
| `--headless-mode` | `headed` \| `new` \| `full` | `headed` | `headed` = visible; `new` = headless-new (looks like a real browser); `full` = classic headless. |
| `--browser` | `edge` \| `chrome` \| `brave` \| `chromium` | `edge` | Which engine drives all sessions. |
| `--tts` | `auto` \| `soundtools` \| `edge_tts` \| `pyttsx3` | `auto` | Pin one TTS backend or use the fallback chain. |

### What your topic produces

Each run builds everything under `output/<run_id>/`:

```
output/<run_id>/
├── script.json        # generated script (title, narration, captions, image prompts)
├── assets/            # background images from Perchance (bg_00.jpg, …)
├── voiceover.wav      # narrated audio
├── final.mp4          # assembled 1080x1920 short
├── post_url.json      # {"url": …} once published
└── run_state.json     # per-stage ledger → enables --resume
```

Resume is resumable: a run that died at, say, the TTS stage restarts at voiceover,
not from scratch. The engine verifies what's already present before re-running a
stage.

### Schedules, locks and exit codes

- **Whole-run lock** (`output/run.lock`) — one workflow at a time. A second run
  while one is live fails fast instead of sharing/corrupting Chromium profiles. If
  a run crashes, the lock ages out (`RUN_LOCK_STALE_S`, default 3600s) and is
  reclaimed automatically.
- **Exit codes:** `0` success · `1` an expected/handled failure · `130`
  interruption. A scheduler can branch on these.
- **Minimum spacing:** at least **2× your longest run**, realistically **2–3 h**.
  A run that starts before the previous one ends only waits on the lock.
- **Unattended = headless.** Run with `--headless` so a CAPTCHA/challenge fails
  fast (`ChallengeError`) instead of sitting invisibly waiting for a human.
  Challenges are *never* auto-solved anywhere (see Safety).
- Log each invocation separately: `python -m automato run "…" -v >> runs.log 2>&1`.

### Dry-run the fully-local stage

The assembly step needs no account or third-party site:

```powershell
python -m automato run "test" --workflow <your-manifest>   # or drive assembly.py directly
```

If you only want to verify rendering/packaging without touching the network, point
a tiny manifest at the assembly adapter with canned inputs (`script.json`, images,
a WAV) — the stage is pure local FFmpeg + Pillow.

---

## Logging in

Only **YouTube requires a signed-in session**. Open a visible browser, sign in,
close it; the session persists in `profiles/`:

```powershell
python -m automato login youtube      # required
python -m automato login ai_studio    # optional (preferred scripting provider)
python -m automato login perchance    # not needed — public generator
```

Scripting **defaults to the no-login chain** (`gemini` preferred) — Google gated
the free AI Studio Playground's default model behind a Google AI Plan / API key, so
AI Studio is only used when you pin `AUTOMATO_LLM_PROVIDER=ai_studio` with a paid
plan. You can ship a fully working pipeline with *zero* sign-ins except YouTube:

1. `gemini` (Gemini web guest, base Flash) — no account, default
2. `duckai` (duck.ai)
3. `ask_brave` (Ask Brave) — no account
4. `chatgpt` (ChatGPT web guest) — no account, region-gated, **last resort**

Set your own order with `AUTOMATO_LLM_NO_LOGIN_CHAIN="duckai,gemini,ask_brave,chatgpt"`.
The winning provider is reported in the run log.

---

## Personalization

### Per-run flags

The fastest personalization is flags, no file edits:

```powershell
python -m automato run "5 high-protein meal-prep ideas" --visibility private --tts edge_tts
python -m automato run "mindfulness in 60 seconds" --browser chromium --headless
python -m automato run "history of tea" --workflow tea_brand
```

### Your niche via a workflow file

Copy the stock manifest and give the niche its own pipeline description,
background count, and prompt curve:

```powershell
Copy-Item workflows/faceless_short.json workflows/tea_brand.json
# edit workflows/tea_brand.json …
python -m automato run "history of tea" --workflow tea_brand
```

Binding rules inside a manifest matter:

- A dotted value such as `"voiceover.audio"` = "the artifact named `audio` emitted
  by stage `voiceover`".
- A non-dotted value such as `"assets"` = "`run_dir/assets`".
- A mistyped binding fails loudly at startup (no silent fall-through).

`image_count` on the assets stage controls how many backgrounds a run wants.
Change the default manifest in `config.py` (`DEFAULT_WORKFLOW`) to stop passing
`--workflow` every time.

### Tuning speech

- **Backend** — `--tts auto` tries SoundTools (browser) → `edge_tts` → `pyttsx3`.
  `--tts edge_tts` pins Microsoft Edge's neural TTS: fast, offline-ish, no browser.
- **Voice** — set `EDGE_TTS_VOICE = "en-US-ChristopherNeural"` in `config.py` to
  any edge-tts voice that fits the tone.
- **Language-aware routing** — general narration always uses the local chain. Text
  in Devanagari (`sa`, Sanskrit) can route to the online **vagdhenu** (IISc) engine,
  but that is opt-in (`AUTOMATO_VAGDHENU_ENABLED=1`) and *not yet wired for real
  speech* — it fails cleanly back to the local chain today. The mapping lives in
  `automato/adapters/tts/routing.py`; adding a new language is a one-line table
  entry plus a provider branch.

### Shaping the images (Perchance)

Perchance is free and unlimited, but you get most from its *native batch* control:

- **Portrait by default** (`SHAPE 512x768`) — native 2:3 vertical, ideal for
  Shorts; assembly just upscales to 1080x1920 (no letterbox).
- **Batching** (`numImages=4`) — 4 finished variations of a single prompt render
  in parallel; the pipeline saves every one and moves on. A 6-image run now uses
  ~2 prompts instead of 6 sequential ones. Override with
  `AUTOMATO_PERCHANCE_SHAPE` / `AUTOMATO_PERCHANCE_NUM_IMAGES` (0 disables).

### Browser variants

- `--browser edge` (default) / `chrome` reuse your installed browser via
  Playwright channel — no downloads.
- `--browser brave` auto-finds an installed Brave (`AUTOMATO_BRAVE_PATH` overrides).
- `--browser chromium` uses the Playwright-bundled build — run
  `playwright install chromium` once. Use it on CI / machines without Edge/Chrome.

Profiles are shared across all variants (they live in `profiles/<provider>/`, not
in any browser binary).

### Config + environment reference

Most knobs live in `automato/config.py`. Every knob has an environment override —
the full, current list:

| Env var | Controls |
|---|---|
| `AUTOMATO_VISIBILITY` | `public\|unlisted\|private` default for `run`. |
| `AUTOMATO_TTS_PROVIDER` | TTS backend default (auto chain). |
| `AUTOMATO_LLM_PROVIDER` | Preferred scripting provider (`gemini` — no login — by default; `ai_studio` requires a paid-gated login). |
| `AUTOMATO_LLM_NO_LOGIN_CHAIN` | Comma-separated order of the no-login fallbacks. |
| `AUTOMATO_BROWSER` / `AUTOMATO_BRAVE_PATH` | Engine + Brave binary path. |
| `AUTOMATO_HEADLESS_MODE` | `headed\|new\|full` default. |
| `AUTOMATO_PERCHANCE_SHAPE` / `AUTOMATO_PERCHANCE_NUM_IMAGES` | Portrait value / batch count; `0` disables batching. |
| `AUTOMATO_VAGDHENU_ENABLED` | Opt-in to the Sanskrit vagdhenu route (currently stubbed). |
| `AUTOMATO_RECOVERY_ENABLED` | Master switch for the LLM recovery agent. |
| `AUTOMATO_CHALLENGE_CHECK` | Master switch for CAPTCHA/login detection. |
| `AUTOMATO_ANTI_AUTOMATION` | Anti-bot flag injection (recommended). |
| `AUTOMATO_MODAL_DISMISS_ENABLED` | `0` disables automatic modal sweeping. |
| `AUTOMATO_ALLOW_EXTERNAL_ADAPTERS` | `1` lets workflows import fully-qualified external adapter modules. |
| `AUTOMATO_BACKUP_PASSPHRASE` | AES passphrase for backups (else one is generated & printed). |

Tuning timers (timeouts, retry backoff, settle intervals, deadline seconds) are
all in `config.py` with plain names like `RETRY_BASE_DELAY_S`,
`GENERIC_LLM_POLL_DEADLINE_S`, `PERCHANCE_MAX_PER_IMAGE_S`, `FFMPEG_TIMEOUT_S`.

---

## Editing the engine (advanced)

### Swap or add an adapter

1. Add `automato/adapters/<category>/<name>.py` exposing
   `run(ctx, inputs, run_dir, session=None) -> dict` returning named artifacts.
2. Register its provider mapping in `automato/orchestrator.py`
   (`_ADAPTER_PROVIDER`) and an optional auth check in `automato/providers.py`.
3. Point a workflow stage at it (`"adapter": "assembly.my_encoder"`).

Workflows resolve adapters under `automato.adapters.*` by default. Fully-qualified
*external* modules are blocked unless you opt in with
`AUTOMATO_ALLOW_EXTERNAL_ADAPTERS=1`.

### Rewrite the scripting prompt

`automato/llm/script_prompts.py` is the exact instruction set the LLM receives.
It emits a delimited plain-text format (`TITLE` / `NARRATION` / `CAPTION` /
`IMAGE` lines ended with `END`), parsed locally by `_parse_script` in
`generic_llm.py`. Change the niche tone here — and keep the `KEY | value` grammar
in sync if you change the format.

### Survive a site changing its UI

- **`health-check`** re-validates each provider's *static* locators in a real
  browser, independent of content runs (learned overlays excluded on purpose).
  Run monthly or on a schedule: `python -m automato health-check
  --provider youtube` etc. Requires login for account-backed providers.
- **`trend`** summarizes the LLM-recovery counter (`output/recovery_trend.json`)
  over N days (`--days`, default 14) and flags providers whose recovery rate
  jumped ~3× week-over-week — your cue to update static locators, not patch
  per-run.
- **`replay-import`** converts a Chrome **DevTools Recorder export**
  (`@puppeteer/replay` JSON) into locator hints: record the click path once, then
  `python -m automato replay-import captured.json --provider youtube`. New
  selectors merge into the learned overlay; `--force` replaces.

Learned selectors are a *cache*: they expire (`LEARNED_SELECTOR_TTL_DAYS`, 30) or
after 20 consecutive static-success resolutions, so a stuck site is re-learned,
never permanently shadowed.

---

## Backups & portability

```powershell
# AES-encrypted snapshot (passphrase: AUTOMATO_BACKUP_PASSPHRASE, else generated & printed)
python -m automato backup

# useful variants
python -m automato backup --no-outputs          # sessions + config only
python -m automato backup --keep 5              # prune older archives to newest 5
python -m automato backup --out C:\exp\v2.zip   # explicit destination

# ship it
python -m automato restore v2.zip --dir C:\Users\Me\engines\my_niche
python -m automato restore v2.zip --dir … --force --apply-config
```

- Archives hold **live signed-in sessions** (Cookies, Login Data, Preferences,
  storage) — treat a backup like your passwords. Volatile caches and session LOCK
  files are excluded.
- Restore **rebinds absolute paths** to the destination root, so a machine-A
  backup restores cleanly onto machine B. Always decrypt-verified against a
  per-file SHA-256 manifest.
- `restore` is a destructive overwrite by design; populated destinations need
  `--force`. `--apply-config` re-applies the source config on top.
- **Portable recipe:** backup baseline → edit a copy (or restore to a sandbox
  `--dir`) → backup the tailored sandbox → ship to another machine.

Full how-to (forking, diffing configs, relocating): [`GUIDE_CUSTOMIZATION.md`](GUIDE_CUSTOMIZATION.md).

---

## Use cases

1. **Personal niche channel** — one login, one command per video; rotate topics
   and keep everything `private`/`unlisted` while you refine the workflow. Disclose
   AI content in the description (see Safety).
2. **Unattended schedule** — `--headless` + a cron/Task Scheduler job, spaced at
   least 2× the longest run, with its own log file; exit code drives alerting.
   Backup on the same cadence.
3. **Niche experiments without risk** — fork via `backup` → `restore --dir`, run a
   throwaway `--visibility private` video against the fork. The proven install
   stays untouched.
4. **Drift monitoring** — monthly `health-check` + `trend` reads tell *you* when a
   third-party site moved, instead of finding out mid-run.
5. **Machine relocation / CI** — `chromium` browser + `backup`/`restore` makes the
   whole thing (sessions included) portable across machines.
6. **Local-only render tests** — exercise the assembly adapter against canned
   inputs with zero network or accounts.

---

## FAQ

**Does this use my account's API billing?** No. It scripts the same web UIs a human
uses. You sign in to your own accounts; the only "cost" is your own browser
sessions. Assembly is local.

**Do I need to sign in to anything besides YouTube?** No. Scripting defaults to
Gemini web guest (no login), falling through to duck.ai → Ask Brave → ChatGPT.
AI Studio is only used if you pin `AUTOMATO_LLM_PROVIDER=ai_studio`. Perchance,
SoundTools, and edge-tts need nothing.

**Where do my sessions live?** `profiles/<provider>/` (local-only, never
committed — the pre-commit guard blocks it). Backups bundle these encrypted.

**Why is everything unlisted by default?** Because publishing to a platform you
automate invites real policy questions (see Safety). `unlisted` means the video
exists but isn't public; flip to `public` only when you're certain.

**A run crashed — do I start over?** No: `python -m automato run "<topic>"
--resume <run_id>` resumes at the first incomplete stage. The whole-run lock is
reclaimed automatically once it ages out.

**I got a "run.lock" error.** Another run is live (or a dead lock under
`RUN_LOCK_STALE_S`). Wait, or after the staleness window the lock is reclaimed.

**Can I run two videos at once?** No — by design. Sharing one set of Chromium
profiles across parallel runs corrupts sessions. Use a separate installed copy if
you truly need parallelism (backup/restore a second root).

**The model ignored my script format.** Loosen the preamble in
`script_prompts.py` and/or widen `_parse_script` in `generic_llm.py`. The engine
tries the next provider automatically when parsing fails.

**How do I know a provider's page changed?** `health-check` + `trend` (above). A
provider whose recovery spike has gone up ~3× week-over-week is your candidate.

**Where do passphrases go?** Set `AUTOMATO_BACKUP_PASSPHRASE` for non-interactive
backups; otherwise a fresh one is generated and printed — store it, it's the only
way to decrypt.

---

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| `AuthRequiredError` on publish | Not signed into YouTube. `python -m automato login youtube`, re-run or resume. |
| "Could not locate the browser" | Edge/Chrome missing. `--browser chromium` + `playwright install chromium`. |
| Run hangs showing a CAPTCHA | Challenge detection paused the run for a human (by design, never auto-solved). In headless it fails fast; in headed mode solve it, or set `AUTOMATO_CHALLENGE_CHECK=0` only if you know the site. |
| `run.lock` refuses a second run | Another run/process is live, or a stale lock within its window. Wait out `RUN_LOCK_STALE_S` or clear `output/run.lock` if you're sure no process is running. |
| Scripting produced no parseable script | Model ignored the delimited format. It retries and falls through the chain; improve the preamble in `script_prompts.py` for permanent fixes. |
| AI Studio shows "Gemini 3 Flash Preview … Google AI Plan or API key" | Google gated the free Playground's default model. The engine already defaults away from AI Studio (`gemini` web guest first). Pin `AUTOMATO_LLM_PROVIDER=ai_studio` only if your account has a paid Google AI plan. |
| Images identical or too slow | Batching mis-set: `AUTOMATO_PERCHANCE_NUM_IMAGES=0` disables batching (slower but more varied); raise it for speed. Shape `512x768` = portrait. Genuine "same image" repeats are per-prompt variation noise — vary the prompt string. |
| Voiceover silent / all providers failed | Works offline-only `--tts pyttsx3` last; check `ffprobe -i voiceover.wav` exists and `EDGE_TTS_VOICE` is a valid voice; network needed for SoundTools/edge-tts. |
| Backup won't decrypt on another machine | Passphrase mismatch or tampering. Restore refuses CRC/checksum mismatches by design — re-export with the correct passphrase. |
| Restore says "destination populated" | `restore` is destructive by default; pass `--force` when you truly want to overwrite, or restore into a fresh `--dir`. |
| Provider lost its locators | Run `health-check --provider <p>` to see exactly which locator groups fail, then `replay-import` the new flow or update the `LOCS` table in its adapter. |
| Everything slowed down / recovery climbing | `trend --days 14` shows the spike; that's the "site changed" early-warning → update static locators. |
| PowerShell vs bash errors | Windows drive letters and quoting differ; copy the block matching your shell. `python -m automato …` in a venv needs its python on PATH. |

---

## Safety, policy & compliance

This is *generic automation tooling* — it scripts the same UI a human would use.
It is **not** a guarantee of compliance with the platforms you publish on. Realistic
policies worth naming before you opt into `--visibility public` or an aggressive
schedule:

- **YouTube monetization (Repetitious Content policy):** mass-produced, reused,
  or templated/repetitive content is not eligible for monetization, and
  near-identical repeat posts can be restricted or removed. A scheduled run of the
  *same* workflow with a canned topic string is exactly what this policy targets.
- **AI-generated content disclosure:** YouTube (and other platforms) require
  disclosing altered/synthetic media — AI narration, imagery, deepfake-looking
  material. This pipeline's script, images, and TTS are all AI-generated, so
  disclosure belongs in the description/title metadata.
- **Comparative/mass-account abuse:** thousands of identical accounts posting on a
  cadence triggers anti-abuse systems. Spread accounts, vary content, and treat
  publishing as a human responsibility.

The project encodes safe defaults: `unlisted` is the default, and CAPTCHA/login
challenges are **never** auto-solved (fail-fast instead). Keep those instincts:
disclose synthetic media, iterate on real content, and go public only deliberately.

## Accepted trade-offs (conscious, monitored choices — R3-F4)

- **The core dependency is five third-party UIs, outside this project's control.**
  They change — hence the resilience layer, recovery-trend telemetry, and a
  scheduled `health-check` instead of pretending drift can't happen.
- **Automating a platform's own web UI carries inherent ToS/account risk.** An
  official API would need keys, quotas, and billing; this project deliberately
  stays API-free. YouTube's Repetitious Content / AI-disclosure and anti-abuse
  policies (above) are the concrete versions of that risk; safe defaults are the
  mitigation.
- **Recovery is an LLM-driven repair, not a guarantee.** When it fails, the run
  fails loudly — never silently passing a bad step — and `trend` makes a rising
  recovery rate visible so a human updates static locators.
- **Learned-selector overlay is a cache, not a source of truth.** Entries expire
  after `LEARNED_SELECTOR_TTL_DAYS` (30) or 20 consecutive static-success
  resolutions. A stuck site is re-learned, never permanently shadowed.
- **Modal dismissal is conservative, audited, and opt-outable.** Provider-scoped
  lists run before a generic baseline; every dismissal logs what was near the
  element it clicked; `AUTOMATO_MODAL_DISMISS_ENABLED=0` disables it.

This is the practical meaning of "no weaknesses remain" for a tool whose core
dependency is outside its own control: everything left is either fixed, mitigated,
or an explicitly monitored, accepted trade-off.

---

## Further reading

| Doc | Covers |
|---|---|
| [`GUIDE_CUSTOMIZATION.md`](GUIDE_CUSTOMIZATION.md) | Edit-vs-fork strategy, per-niche workflows, backup/restore deep dive. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Internals: orchestrator, manifest resolution, resilience, recovery. |
| [`SECURITY.md`](SECURITY.md) | Sessions, encryption, challenge handling, threat model. |
| `automato/config.py` | Every tunable constant + env override, in one file. |