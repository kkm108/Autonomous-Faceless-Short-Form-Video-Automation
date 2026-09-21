# AutoVideo — Complete Guide
### From First Install to Production Operations

---

> **Who this guide is for**
> This guide starts at absolute zero — no assumptions about prior experience with Python, video production, or APIs. It walks you through installation, your first video, day-to-day operation, advanced customisation, and developer-level extension. Skip to whichever section matches where you are.

---

## Table of Contents

1. [What AutoVideo Does](#1-what-autovideo-does)
2. [How It Works — Plain English](#2-how-it-works--plain-english)
3. [Installation](#3-installation)
4. [Your First Video](#4-your-first-video)
5. [Everyday Operation](#5-everyday-operation)
6. [Input Types](#6-input-types)
7. [Configuration Reference](#7-configuration-reference)
8. [API Keys — Which Ones You Need](#8-api-keys--which-ones-you-need)
9. [Voice Providers](#9-voice-providers)
10. [Visual Modes](#10-visual-modes)
11. [Distribution — Getting Videos Where They Need to Go](#11-distribution--getting-videos-where-they-need-to-go)
12. [Monitoring and Observability](#12-monitoring-and-observability)
13. [Scaling Up](#13-scaling-up)
14. [Advanced Tweaks](#14-advanced-tweaks)
15. [Adding a New Component — Developer Reference](#15-adding-a-new-component--developer-reference)
16. [Troubleshooting](#16-troubleshooting)
17. [FAQ](#17-faq)
18. [Glossary](#18-glossary)

---

## 1. What AutoVideo Does

AutoVideo takes a **topic, a URL, an RSS feed, or a pre-written script** and produces a **complete, platform-ready short-form video** — the kind of content published on YouTube Shorts, Instagram Reels, and TikTok — with zero manual editing between submission and delivery.

Every output consists of three files:

| File | What it is |
|---|---|
| `video.mp4` | H.264 video, 1080×1920 (portrait), with burned-in animated captions |
| `video_thumb.jpg` | Thumbnail at 1280×720, extracted automatically |
| `video_metadata.json` | Title, description, hashtags, chapter timestamps, and platform-specific hints ready for a social media scheduler |

The entire pipeline — writing the script, generating the voiceover, finding background visuals, composing the video, and uploading to your configured destinations — runs without any human intervention once you have submitted a job.

---

## 2. How It Works — Plain English

When you submit a topic like `"The science of black holes"`, the following happens automatically in order:

```
Your input
    │
    ▼
 ① Ingestion     Figures out what kind of input you gave
                 (topic string, URL, RSS feed, or pre-written script)
    │
    ▼
 ② Script        Claude writes a structured narration broken into scenes,
                 with visual search terms, hashtags, and metadata.
                 No API key? A built-in template writer runs instead.
    │
    ▼
 ③ Voice         Text-to-speech narration is generated.
                 Tries ElevenLabs → Google TTS → Coqui → pyttsx3 in order.
                 One always works — no API key is required.
    │
    ▼
 ④ Assets        For each scene, finds a matching video clip or image.
                 Tries Pexels → Pixabay → Stability AI → coloured placeholder.
                 One always works — no API key is required.
    │
    ▼
 ⑤ Captions      Word-by-word karaoke-style subtitles are generated from
                 the voice timing data and baked permanently into the video.
    │
    ▼
 ⑥ Audio mix     Narration is combined with a background music track
                 (if you have added music files). Narration always stays
                 clearly louder than the music.
    │
    ▼
 ⑦ Render        FFmpeg composes all scenes into a single H.264/AAC MP4.
    │
    ▼
 ⑧ Thumbnail     A frame is extracted at 10% of the video's duration.
    │
    ▼
 ⑨ Metadata      Platform-optimised title, description, hashtags, and
                 chapter timestamps are written to a companion JSON file.
    │
    ▼
 ⑩ Distribute    The finished files are copied to your configured
                 destinations (local folder, S3, Google Drive, Dropbox, FTP).
```

If any step fails, the system falls back to a simpler alternative rather than stopping. If the system crashes mid-render, it picks up from where it left off when restarted — no completed work is repeated.

---

## 3. Installation

### Step 1 — Install FFmpeg

FFmpeg is required. It is free and handles all video encoding.

**Windows:**
1. Download from https://ffmpeg.org/download.html — choose "Windows builds by gyan.dev" and pick the "full" build
2. Extract the zip file and copy the contents of the `bin` folder to `C:\ffmpeg\bin`
3. Add `C:\ffmpeg\bin` to your system PATH:
   - Open Start → search "Environment Variables" → click "Edit the system environment variables"
   - Click "Environment Variables" → find "Path" under System Variables → Edit → New → `C:\ffmpeg\bin`
4. Open a **new** terminal and run `ffmpeg -version` — you should see a version number

**macOS:**
```bash
brew install ffmpeg
```

**Ubuntu / Debian:**
```bash
sudo apt update && sudo apt install ffmpeg
```

### Step 2 — Install Python packages

```bash
pip install -r requirements.txt
```

If you are on **Python 3.13 or later** and see an error mentioning `audioop`:
```bash
pip install audioop-lts
```

### Step 3 — Set up your configuration file

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` in any text editor. At minimum, read [Section 8](#8-api-keys--which-ones-you-need) to understand which API keys you need (zero are required to produce your first video).

### Step 4 — Verify the installation

```bash
python validate.py --skip-render
```

All checks should show `✓` or `⚠` (warning). A `✗` (failure) means something needs fixing — the message tells you exactly what to do.

To run a complete end-to-end test that produces a real video:
```bash
python validate.py
```

A passing run means your installation is fully functional.

---

## 4. Your First Video

### The simplest possible command

```bash
python -m autovideo submit "How black holes form" --run
```

`--run` means "start processing immediately after queuing." Without it, the job is added to the queue but waits until you separately start the worker pool.

You will see log output as each of the 10 stages completes. When finished, look in the `./output/` folder for your three output files.

### Step by step, if you prefer more control

**Queue the job:**
```bash
python -m autovideo submit "How black holes form"
# ✓ Queued job a3f2b1c8 — How black holes form
```

**Start the worker pool** (in the same or another terminal):
```bash
python -m autovideo run
# ▶ Worker pool starting (4 workers) …  Ctrl-C to stop
```

**Check progress** (in another terminal):
```bash
python -m autovideo status
```

**Find your files when done:**
```
./output/
  how-black-holes-form_20260702_120000_a3f2b1c8.mp4
  how-black-holes-form_20260702_120000_a3f2b1c8_thumb.jpg
  how-black-holes-form_20260702_120000_a3f2b1c8_metadata.json
```

---

## 5. Everyday Operation

All commands follow the pattern: `python -m autovideo <command> [options]`

---

### `submit` — Queue a job

```bash
python -m autovideo submit "Your topic here"
python -m autovideo submit "Your topic here" --run            # queue and process immediately
python -m autovideo submit "Your topic here" --duration 45   # 45-second video
python -m autovideo submit "Your topic here" --language ar   # Arabic
python -m autovideo submit "Your topic here" --visual-mode cinematic
python -m autovideo submit https://example.com/article        # from a URL
python -m autovideo submit https://example.com/feed.rss       # from an RSS feed
cat script.txt | python -m autovideo submit -                 # from a file via stdin
```

| Option | Default | Description |
|---|---|---|
| `--language`, `-l` | `auto` | Language code (`en`, `ar`, `fr`, `de`, `hi` …) or `auto` to detect automatically |
| `--visual-mode`, `-m` | `dark_narration` | Visual style: `dark_narration`, `branded`, or `cinematic` |
| `--duration`, `-d` | `60` | Target video length in seconds |
| `--run` | off | Start a worker immediately after submitting |
| `--config`, `-c` | `config.yaml` | Path to an alternative config file |

---

### `batch` — Queue many jobs at once

Create a plain text file with one input per line:

```
# topics.txt
# Lines starting with # are ignored. Blank lines are ignored.
How black holes form
The history of the Roman Empire
https://example.com/article-about-ai
Why do cats purr
```

Submit the entire file:
```bash
python -m autovideo batch topics.txt
python -m autovideo batch topics.txt --duration 45 --visual-mode cinematic
```

---

### `run` — Start the worker pool

```bash
python -m autovideo run               # uses max_workers from config.yaml
python -m autovideo run --workers 8   # temporarily override worker count
```

The pool runs until you press `Ctrl-C`. Any job currently being processed is cleanly marked for retry on next startup.

---

### `status` — See what is happening

```bash
python -m autovideo status
python -m autovideo status --limit 50   # show last 50 jobs instead of 20
```

Displays a queue summary (pending / processing / done / failed / retrying counts, average render time) plus a table of recent jobs.

---

### `health` — Check all components

```bash
python -m autovideo health
```

Checks that FFmpeg, the database, and each configured API service are reachable. Exits with code `0` if everything required is healthy, `1` if not — suitable for use in monitoring scripts and CI pipelines.

---

### `dashboard` — Live view

```bash
python -m autovideo dashboard
```

A live-updating terminal dashboard that refreshes every two seconds. Shows queue counts, recent jobs, recent errors, and system health all in one screen. Press `Ctrl-C` to exit.

---

### `job` — Inspect a specific job

```bash
python -m autovideo job a3f2b1c8       # first 8 characters of the ID
python -m autovideo job a3f2b1c8-4db3-4ef8-8573-194cbf834f26  # full ID
```

Prints the complete job record as JSON, including which of the 10 pipeline stages have completed — useful for diagnosing exactly where a stalled job stopped.

---

## 6. Input Types

AutoVideo detects what kind of input you are giving it automatically.

### Topic string

Any plain text that is not a URL, not a multi-line block, and not clearly a script.

```bash
python -m autovideo submit "Why is the sky blue"
python -m autovideo submit "5 surprising facts about the deep ocean"
```

The LLM (or template generator) writes the full narration from scratch.

### URL

Any `http://` or `https://` link to a web page that is not an RSS feed.

```bash
python -m autovideo submit "https://www.bbc.com/news/science-environment-12345678"
```

The article is fetched, navigation and advertisements are stripped, and the body text is condensed into a narration script by the LLM.

### RSS feed

URLs containing `/feed`, `/rss`, or ending in `.rss` or `.xml`.

```bash
python -m autovideo submit "https://feeds.bbci.co.uk/news/science/rss.xml"
```

Each item in the feed becomes a **separate job**. The feed title becomes the series title in the metadata of every video.

RSS field mapping:

| RSS field | Used as |
|---|---|
| `entry.title` | Video topic and title |
| `entry.summary` or `entry.description` | Narration seed — expanded by the LLM |
| `entry.link` | Stored in metadata as source URL |
| `entry.published` | Stored in metadata |
| `feed.title` | Series title across all videos from this feed |

### Pre-written script

Any multi-line text, or text longer than 200 characters.

```bash
cat my_script.txt | python -m autovideo submit -
```

Or type it directly:

```bash
echo "Paragraph one of the narration here.
Paragraph two with more details about the topic.
A closing thought to wrap everything up." | python -m autovideo submit -
```

The text is used **verbatim** as the narration. The LLM still handles scene-splitting, visual search queries, and metadata — it does not rewrite or paraphrase your words.

---

## 7. Configuration Reference

`config.yaml` controls every aspect of the system. All values support environment variable substitution using the `${VAR_NAME}` syntax — the value is read from your environment at startup.

---

### Anthropic (LLM for script and metadata)

```yaml
anthropic:
  api_key: ${ANTHROPIC_API_KEY}
  model: claude-sonnet-4-6
  max_tokens: 4096
```

If `api_key` is blank or the environment variable is not set, the template generator handles scripts automatically. No crash, no manual intervention needed.

---

### Voice chain

```yaml
voice:
  chain:
    - provider: elevenlabs
      api_key: ${ELEVENLABS_API_KEY}
      voice_id: 21m00Tcm4TlvDq8ikWAM   # Rachel, multilingual
      model_id: eleven_multilingual_v2
    - provider: google
      credentials_json: ${GOOGLE_APPLICATION_CREDENTIALS}
    - provider: coqui
      model: tts_models/multilingual/multi-dataset/xtts_v2
      device: cpu                        # or: cuda
    - provider: pyttsx3
      rate_wpm: 150
```

Providers are tried top to bottom. Remove any entry whose key you do not have. The list must keep at least one entry — `pyttsx3` requires no key and is always the safe backstop.

---

### Asset chain

```yaml
assets:
  stock_chain:
    - provider: pexels
      api_key: ${PEXELS_API_KEY}
      per_page: 10
    - provider: pixabay
      api_key: ${PIXABAY_API_KEY}
      per_page: 10
  generation_chain:
    - provider: stability
      api_key: ${STABILITY_API_KEY}
      engine: stable-diffusion-xl-1024-v1-0
    - provider: placeholder         # always available, no key needed
  cache_dir: ./asset_cache          # downloaded assets cached here
```

---

### Rendering

```yaml
rendering:
  output:
    width: 1080
    height: 1920      # 9:16 portrait — required for Shorts/Reels/TikTok
    fps: 30
    video_bitrate: "4M"
    audio_bitrate: "192k"
    preset: fast      # ultrafast / superfast / fast / medium / slow
    crf: 23           # quality: 18 = near-lossless, 28 = smaller file
    pix_fmt: yuv420p  # required for maximum platform compatibility
```

**Choosing a preset:**

| Preset | Render speed | File size | Quality |
|---|---|---|---|
| `ultrafast` | Fastest | Largest | Good |
| `fast` | Fast | Medium | Good |
| `medium` | Moderate | Smaller | Better |
| `slow` | Slow | Smallest | Best |

For continuous batch production, `fast` is the best balance. For archive-quality output, use `medium` or `slow`.

**CRF (quality number):**
- `18` — near-lossless, largest file
- `23` — default, excellent quality
- `28` — noticeably smaller file, slight compression on detailed scenes

---

### Captions

```yaml
rendering:
  captions:
    font: Arial
    font_size: 72               # pixels; increase for small-screen readability
    primary_colour: "&H00FFFFFF"    # white text (ASS BGR hex format)
    highlight_colour: "&H0000FFFF"  # yellow — the word currently being spoken
    back_colour: "&H80000000"       # semi-transparent black background box
    bold: true
    outline: 3                  # outline thickness in pixels
    shadow: 2
    margin_v: 80                # distance from bottom of frame in pixels
```

**ASS colour format** is `&HAABBGGRR` (Blue-Green-Red, not RGB — a quirk of the format):

| Colour | Value |
|---|---|
| White | `&H00FFFFFF` |
| Yellow | `&H0000FFFF` |
| Cyan | `&H00FFFF00` |
| Green | `&H0000FF00` |
| Red | `&H000000FF` |
| Blue | `&H00FF0000` |

---

### Audio mixing

```yaml
audio:
  narration_volume: 1.0    # 1.0 = full; 0.8 = 80%; 1.2 = 120%
  music_volume: 0.12       # 12% of full — keeps narration clearly dominant
  music_dir: ./music       # folder of .mp3 or .wav background tracks
  fade_in_ms: 500          # music fades in over 500ms at the start
  fade_out_ms: 1000        # music fades out over 1 second at the end
```

If `music_dir` is absent or the folder is empty, videos render with narration only. This is not an error — it is an expected and handled fallback.

---

### Queue

```yaml
queue:
  db_path: ./autovideo.db    # SQLite database; created automatically
  max_workers: 4             # concurrent video renders
  max_retries: 3             # how many times to retry a failed job
  retry_delay_s: 30          # seconds to wait before retrying
  work_dir: ./work           # temporary files during rendering
```

---

### Distribution

```yaml
distribution:
  local:
    output_dir: ./output     # always active; videos always appear here

  s3:
    enabled: false
    bucket: my-video-bucket
    prefix: videos/
    region: us-east-1
    access_key: ${AWS_ACCESS_KEY_ID}
    secret_key: ${AWS_SECRET_ACCESS_KEY}
    endpoint_url: null       # null = AWS; set URL for MinIO/Cloudflare R2/etc.

  gdrive:
    enabled: false
    folder_id: ""            # the ID from the Google Drive folder URL
    credentials_file: ${GDRIVE_CREDENTIALS}

  dropbox:
    enabled: false
    access_token: ${DROPBOX_ACCESS_TOKEN}
    path: /AutoVideo

  ftp:
    enabled: false
    host: ""
    port: 21
    username: ${FTP_USER}
    password: ${FTP_PASS}
    tls: true
```

Set `enabled: true` for any destination you want to activate. A failed cloud upload is always logged but never prevents the local copy from completing.

---

### Observability

```yaml
observability:
  log_level: INFO            # DEBUG | INFO | WARNING | ERROR
  log_file: ./autovideo.log
  log_format: json           # json (structured) | text (human-readable)
  dashboard_refresh_s: 2
```

---

## 8. API Keys — Which Ones You Need

You need **zero API keys** to produce your first video. Every subsystem has a built-in fallback that needs no credentials. Quality improves with each key you add.

| Key | Service | Effect if missing | Where to get it | Cost |
|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | Claude LLM | Template script generator used; simpler metadata | console.anthropic.com | Pay per use |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS | Next provider tried | elevenlabs.io | Free tier available |
| `GOOGLE_APPLICATION_CREDENTIALS` | Google Cloud TTS | Next provider tried | console.cloud.google.com | Free tier available |
| `PEXELS_API_KEY` | Pexels stock footage | Pixabay tried next | pexels.com/api | Free |
| `PIXABAY_API_KEY` | Pixabay stock footage | AI generation tried next | pixabay.com/api | Free |
| `STABILITY_API_KEY` | Stability AI images | Built-in placeholder used | platform.stability.ai | Pay per use |
| `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | S3 upload | S3 disabled; local delivery unaffected | aws.amazon.com | Pay per use |
| `DROPBOX_ACCESS_TOKEN` | Dropbox upload | Dropbox disabled; local delivery unaffected | dropbox.com/developers | Free |

### How to set environment variables

**Windows (Command Prompt — current session only):**
```cmd
set ANTHROPIC_API_KEY=sk-ant-...
set PEXELS_API_KEY=...
python -m autovideo submit "My topic" --run
```

**Windows (PowerShell — current session only):**
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python -m autovideo submit "My topic" --run
```

**macOS / Linux (current session only):**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python -m autovideo submit "My topic" --run
```

**Permanent — all platforms:** Add the export/set lines to your shell profile (`.bashrc`, `.zshrc`, or Windows System Environment Variables). Alternatively, put the key values directly in `config.yaml` where you see the `${...}` placeholders — just keep `config.yaml` private and never commit it to version control.

---

## 9. Voice Providers

AutoVideo tries voice providers in the order they appear in `config.yaml`. The first one that is available and succeeds is used. All providers produce valid audio — the main differences are quality and how accurately captions track the spoken words.

| Provider | Voice quality | Caption timing | Requires | Typical speed |
|---|---|---|---|---|
| ElevenLabs | Excellent, natural | Word-accurate (≤100ms) | API key | 5–15 seconds |
| Google Cloud TTS | Very good, neutral | Word-accurate (≤100ms) | Service account | 3–8 seconds |
| Coqui TTS | Good, slightly robotic | Estimated (char-rate) | Python package | 20–60 seconds |
| pyttsx3 | Basic, OS voice | Estimated (char-rate) | Nothing | 2–5 seconds |

**Word-accurate timing** means each word's subtitle highlight fires within 100ms of when it is spoken.

**Estimated timing** calculates highlight timing from character counts rather than actual audio analysis. It is slightly less accurate but invisible to most viewers at normal playback speed.

### Changing the ElevenLabs voice

1. Go to elevenlabs.io, open the Voices section, and find the voice you want
2. Click the voice and copy its Voice ID
3. In `config.yaml`, replace the `voice_id` value:
```yaml
voice:
  chain:
    - provider: elevenlabs
      voice_id: YOUR_VOICE_ID_HERE
```

### Using only pyttsx3 (no API keys required)

```yaml
voice:
  chain:
    - provider: pyttsx3
      rate_wpm: 150
```

### Installing Coqui for a local neural voice

```bash
pip install TTS
```

The model (approximately 1.8 GB) downloads automatically on first use. Use `device: cuda` if you have an NVIDIA GPU for significantly faster synthesis.

---

## 10. Visual Modes

Each video is rendered in one of three visual modes, selectable per job.

| Mode | Look and feel | Best for |
|---|---|---|
| `dark_narration` | Slightly desaturated, gentle vignette around edges | Educational, documentary, factual content |
| `branded` | Vibrant, more saturated, clean edges | Product content, upbeat topics |
| `cinematic` | Moody, high contrast, vignette | Storytelling, dramatic topics |

Set per job:
```bash
python -m autovideo submit "Ocean exploration" --visual-mode cinematic
```

Set as the default for all jobs in `config.yaml`:
```yaml
content:
  default_visual_mode: cinematic
```

### Adjusting the look of each mode

```yaml
rendering:
  visual_modes:
    cinematic:
      saturation: 0.85    # 1.0 = normal; less than 1.0 = desaturated; more = vivid
      contrast: 1.2       # 1.0 = normal; more than 1.0 = punchier contrast
      vignette: true      # darkened edges around the frame
    branded:
      saturation: 1.15
      contrast: 1.05
      vignette: false
```

### Adding background music by mood

Create a `./music/` folder and drop in `.mp3` or `.wav` files. Name them to include a mood word and they will be matched to the mood detected in each scene:

```
music/
  neutral_ambient_loop.mp3
  energetic_upbeat_track.mp3
  calm_piano_background.mp3
  dramatic_strings.mp3
  inspiring_uplift.mp3
```

Mood tags recognised: `neutral`, `energetic`, `calm`, `dramatic`, `inspiring`.

If a scene has no matching mood file, any track in the music folder is used. If the folder is empty or missing, the video renders with narration only.

---

## 11. Distribution — Getting Videos Where They Need to Go

Videos are always saved to `./output/` first. Cloud destinations are additional and optional. A failure in any cloud upload is logged and skipped — the local copy is never withheld because a cloud upload failed.

### Amazon S3 (and compatible services)

```yaml
distribution:
  s3:
    enabled: true
    bucket: my-video-bucket
    prefix: videos/
    region: us-east-1
    access_key: ${AWS_ACCESS_KEY_ID}
    secret_key: ${AWS_SECRET_ACCESS_KEY}
    endpoint_url: null    # null for AWS; set a URL for other providers:
                          # MinIO:         http://localhost:9000
                          # Cloudflare R2: https://<account>.r2.cloudflarestorage.com
                          # Backblaze B2:  https://s3.us-west-004.backblazeb2.com
```

### Google Drive

1. Create a project at console.cloud.google.com
2. Enable the Google Drive API for that project
3. Create a service account and download its JSON credentials file
4. Share your target Drive folder with the service account's email address

```yaml
distribution:
  gdrive:
    enabled: true
    folder_id: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs"   # from the Drive folder URL
    credentials_file: ./gdrive_credentials.json
```

### Dropbox

1. Go to dropbox.com/developers and create an app
2. Generate an access token under "OAuth 2"

```yaml
distribution:
  dropbox:
    enabled: true
    access_token: ${DROPBOX_ACCESS_TOKEN}
    path: /AutoVideo
```

### FTP

```yaml
distribution:
  ftp:
    enabled: true
    host: ftp.yourserver.com
    port: 21
    username: ${FTP_USER}
    password: ${FTP_PASS}
    path: /public_html/videos
    tls: true
```

---

## 12. Monitoring and Observability

### Quick status check

```bash
python -m autovideo status
```

Shows pending / processing / done / failed / retrying counts, average render time, and a table of your most recent 20 jobs.

### Live dashboard

```bash
python -m autovideo dashboard
```

Auto-refreshes every 2 seconds (configurable). Shows everything in one screen. Press `Ctrl-C` to exit.

### Health check

```bash
python -m autovideo health
```

Reports the status of every subsystem component:

| Component | What it checks |
|---|---|
| `ffmpeg` | Is the FFmpeg binary available and working? |
| `queue_db` | Is the SQLite database reachable? |
| `anthropic_api` | Is the API key valid and the service reachable? (optional) |
| `elevenlabs` | Is the key valid? (optional) |
| `pexels` | Is the key valid? (optional) |
| `pyttsx3_tts` | Is the emergency TTS fallback installed? |

Exits with code `0` if all required components are healthy, `1` otherwise — compatible with Nagios, UptimeRobot command checks, and most CI systems.

### Log file

All activity is written to `./autovideo.log`. Each line is a structured JSON object:

```json
{"ts":"2026-07-02T12:00:00Z","level":"INFO","component":"pipeline","job_id":"abc123","msg":"Job complete in 47.3s"}
{"ts":"2026-07-02T12:00:01Z","level":"ERROR","component":"voice_chain","job_id":"abc123","msg":"ElevenLabs failed","error_type":"HTTPError","input":"Black holes are…","fallback":"trying google_tts"}
```

Every error log entry always includes:
- `component` — which subsystem failed
- `error_type` — the Python exception class name
- `input` — what triggered the failure
- `fallback` — what was tried next (or why nothing was available)

To switch to human-readable text logs:
```yaml
observability:
  log_format: text
```

### Inspecting a job's checkpoint state

```bash
python -m autovideo job a3f2b1c8
```

The `checkpoint` field in the output shows a boolean for each of the 10 pipeline stages. A `false` entry shows exactly where a stalled job stopped:

```json
"checkpoint": {
  "brief_ready": true,
  "script_ready": true,
  "audio_ready": true,
  "assets_ready": true,
  "captions_ready": true,
  "mixed_ready": true,
  "video_ready": false,
  "final_ready": false,
  "metadata_ready": false,
  "distributed": false
}
```

This job stopped during the video render (stage 7) and will resume from there on the next worker restart.

---

## 13. Scaling Up

### Increasing throughput

Change one number in `config.yaml` and restart the worker pool:

```yaml
queue:
  max_workers: 16   # was 4; now renders 16 videos simultaneously
```

**Practical limits on a single machine:**

| Setup | Recommended max_workers |
|---|---|
| Laptop, CPU only | 4–6 |
| Desktop, CPU only | 8–12 |
| Machine with NVIDIA GPU (for Coqui) | 16+ |
| Cloud VM (8-core, 32 GB) | 12–20 |

The queue itself handles thousands of pending jobs without performance problems — the bottleneck is always CPU/GPU for encoding, not the queue.

### Separating queuing from processing

You can run multiple terminals doing different things simultaneously:

```bash
# Terminal 1 — keep submitting new jobs
python -m autovideo batch morning_topics.txt

# Terminal 2 — process the queue
python -m autovideo run --workers 8

# Terminal 3 — watch progress
python -m autovideo dashboard
```

### Using different configs per environment

```bash
# High-quality production runs
python -m autovideo --config production.yaml run --workers 12

# Fast draft runs for review
python -m autovideo --config draft.yaml submit "test topic" --run
```

A `draft.yaml` might set `preset: ultrafast`, `crf: 28`, no cloud distribution, and the template script generator — letting you review content cheaply before committing to full renders.

---

## 14. Advanced Tweaks

### Producing 90-second videos

```yaml
content:
  target_duration_s: 90
```

Or per job:
```bash
python -m autovideo submit "History of the Roman Empire" --duration 90
```

### Faster draft renders

```yaml
rendering:
  output:
    preset: ultrafast
    crf: 28
    video_bitrate: "2M"
```

Roughly halves render time. Quality is good enough to review content but not for final delivery.

### Disabling Ken Burns on still images

```yaml
rendering:
  ken_burns:
    enabled: false
```

Ken Burns is the slow zoom effect applied to photographs. Disabling it speeds up rendering slightly and gives a flatter, more presentational look — sometimes better for screen-capture or text-heavy content.

### Adjusting caption position

Move captions higher up the frame by increasing `margin_v`:
```yaml
rendering:
  captions:
    margin_v: 300   # pixels from the bottom; increase to push captions higher
```

Make captions bigger for viewing on small phones:
```yaml
rendering:
  captions:
    font_size: 90
    outline: 4
```

### Using a custom font for captions

On Windows, any font installed in the system Fonts folder can be used by name. On Linux, install the font and use its family name:

```bash
sudo apt install fonts-noto          # supports many languages including Arabic
```

```yaml
rendering:
  captions:
    font: Noto Sans Arabic            # or: Noto Sans, Liberation Sans, DejaVu Sans
```

### Horizontal video (YouTube long-form)

```yaml
rendering:
  output:
    width: 1920
    height: 1080
```

Note: the platform metadata JSON still defaults to Shorts/Reels/TikTok format. If you change to landscape, review the `platform_hints` section in the output metadata and adjust your scheduler accordingly.

### Purging old completed jobs from the database

The database grows as jobs accumulate over time. To remove completed jobs older than 30 days:

```bash
python -c "
import sqlite3, datetime
cutoff = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat()
conn = sqlite3.connect('autovideo.db')
conn.execute(\"DELETE FROM jobs WHERE status='done' AND finished_at < ?\", (cutoff,))
conn.commit()
print('Deleted:', conn.total_changes, 'rows')
conn.close()
"
```

---

## 15. Adding a New Component — Developer Reference

AutoVideo is built so that every major subsystem can be replaced without touching unrelated code. This section documents how.

### Adding a new TTS voice provider

**1. Implement `VoiceProvider` in `autovideo/voice/providers.py`:**

```python
class MyTTSProvider(VoiceProvider):
    name = "my_tts"
    requires_network = True   # False for local/offline providers

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def is_available(self) -> bool:
        return bool(self._key)

    def synthesise(self, text: str, language: str,
                   output_path: Path) -> SynthesisResult:
        # Call your API, write audio to output_path
        # ...
        # If you have no word-level timestamps, use the helper:
        timings = self.estimate_word_timings(text, duration_seconds)
        quality = TimingQuality.ESTIMATED   # or WORD if you have real timing

        return SynthesisResult(
            audio_path=output_path,
            word_timings=timings,
            timing_quality=quality,
            duration_s=duration_seconds,
            language=language,
            provider_name=self.name,
        )
```

**2. Register it in `autovideo/voice/chain.py` `build_voice_chain()`:**

```python
provider_map = {
    # ... existing entries ...
    "my_tts": lambda c: MyTTSProvider(api_key=c.get("api_key", "")),
}
```

**3. Add to `config.yaml`:**

```yaml
voice:
  chain:
    - provider: my_tts
      api_key: ${MY_TTS_API_KEY}
    - provider: pyttsx3   # keep as backstop
```

No other files need to change.

---

### Adding a new asset (stock footage / image) provider

**1. Implement `BaseAssetProvider` in `autovideo/assets/providers.py`:**

```python
class MyStockProvider(BaseAssetProvider):
    name = "my_stock"

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def is_available(self) -> bool:
        return bool(self._key)

    def fetch(self, query: str, cache_dir: Path,
              width: int, height: int) -> Optional[AssetResult]:
        # Search your API for query
        # Return None if no result found (triggers next provider)
        # Raise an exception on errors (triggers next provider + logs the error)
        # ...
        return AssetResult(
            path=downloaded_path,
            asset_type=AssetType.VIDEO,   # or AssetType.IMAGE
            provider=self.name,
            query=query,
        )
```

**2. Register it in `autovideo/assets/resolver.py` `build_resolver()`:**

```python
for entry in (cfg.get("assets.stock_chain") or []):
    name = entry.get("provider")
    if name == "my_stock":
        stock.append(MyStockProvider(api_key=entry.get("api_key", "")))
```

**3. Add to `config.yaml`:**

```yaml
assets:
  stock_chain:
    - provider: my_stock
      api_key: ${MY_STOCK_KEY}
    - provider: pexels   # keep existing as fallback
```

---

### Adding a new distribution destination

**1. Implement `DistributionTarget` in `autovideo/distribution/targets.py`:**

```python
class MyTarget(DistributionTarget):
    name = "my_service"

    def __init__(self, token: str, path: str) -> None:
        self._token = token
        self._path  = path

    def is_enabled(self) -> bool:
        return bool(self._token)

    def upload_video(self, video_path: Path, metadata: dict) -> str:
        # Upload and return a URL or path string
        ...

    def upload_thumbnail(self, thumb_path: Path, metadata: dict) -> str:
        ...

    def upload_metadata(self, metadata: dict, json_path: Path) -> str:
        ...

    def health_check(self) -> bool:
        ...

    # Optional hooks — these have default no-op implementations in the base class
    def pre_upload(self, metadata: dict) -> None: ...
    def post_upload(self, results: dict) -> None: ...
    def on_error(self, exc: Exception, context: str) -> None: ...
```

**2. Register it in `autovideo/distribution/dispatcher.py` `build_dispatcher()`:**

```python
my = dist_cfg.get("my_service", {}) or {}
if my.get("enabled"):
    targets.append(MyTarget(
        token=my.get("token", ""),
        path=my.get("path", "/videos"),
    ))
```

**3. Add to `config.yaml`:**

```yaml
distribution:
  my_service:
    enabled: true
    token: ${MY_SERVICE_TOKEN}
    path: /videos
```

---

### Pipeline stages — developer reference

| Stage | Checkpoint flag | Input | Output |
|---|---|---|---|
| 1 | *(ingestion, before queue)* | Raw user input | `ContentBrief` |
| 2 | `script_ready` | `ContentBrief` | `FullScript` JSON |
| 3 | `audio_ready` | `FullScript.full_narration` | MP3 + word timings JSON |
| 4 | `assets_ready` | Per-scene visual queries | Asset paths JSON |
| 5 | `captions_ready` | Word timings | ASS subtitle file |
| 6 | `mixed_ready` | Narration MP3 + music | Mixed audio MP3 |
| 7 | `video_ready` | Scene clips + captions + audio | Final MP4 |
| 8 | `final_ready` | MP4 | Thumbnail JPEG |
| 9 | `metadata_ready` | `FullScript` + file paths | Metadata JSON |
| 10 | `distributed` | All output files | Files in destinations |

On crash, the pipeline resumes from the first `False` checkpoint flag. Use `autovideo job <id>` to inspect the checkpoint state of any job.

---

## 16. Troubleshooting

### `validate.py` fails at prerequisites

**FFmpeg not found:**
```
✗  ffmpeg not found in PATH
```
Install FFmpeg and add its `bin` folder to your system PATH. Open a fresh terminal after adding it, then run `ffmpeg -version` to confirm.

**pydub fails on Python 3.13 or later:**
```
✗  pydub installed but requires 'audioop' (removed in Python 3.13+)
```
Run: `pip install audioop-lts`

**Any other missing package:**
```
✗  Missing package: xxx  →  pip install xxx
```
Run the exact `pip install` command shown in the error.

---

### Jobs fail immediately

Find the error:
```bash
python -m autovideo job <job-id>
```

Look at `last_error` and `last_error_component`.

**`voice_chain` / `VoiceChainExhausted`:**
Every TTS provider failed. Ensure `pyttsx3` is installed (`pip install pyttsx3`) — it is the emergency fallback that requires no API key. Run `python -m autovideo health` to see which providers are currently reachable.

**`script_generator` / LLM API error:**
The Anthropic API call failed. Check your key at console.anthropic.com. Note that even if this fails, the template generator should have caught it — if the job hard-failed, check whether both generators raised, which would appear in the log.

**`ffmpeg_engine` / FFmpeg error:**
The log entry includes the full FFmpeg command and its error output. Common causes:
- On Windows, make sure you downloaded the **full** FFmpeg build from gyan.dev — the "essentials" build omits some codecs
- Check that `./work/` and `./output/` directories are writable
- Check available disk space

---

### Jobs stuck in `processing` forever

A worker process was killed without gracefully shutting down. On next start, the queue manager automatically resets all `processing` jobs to `retrying`. Just restart:

```bash
python -m autovideo run
```

Jobs resume from their last checkpoint — they do not start from scratch.

---

### Videos show only a placeholder image

No stock footage was found. The placeholder (a coloured image with the search query text) confirms the video was produced but no visual matched the query.

To fix:
- Add a `PEXELS_API_KEY` (free at pexels.com/api) — resolves the vast majority of queries
- Add a `PIXABAY_API_KEY` as a second source (also free)
- Add a `STABILITY_API_KEY` for AI-generated images when no stock footage matches

---

### Captions are not visible

Captions are burned into the video file, not a separate track. If they are not visible:

1. Check the log for errors from `caption_renderer` or `ffmpeg_engine`
2. Ensure the font named in `config.yaml` exists on your system
   - Windows / macOS: `Arial` is always available
   - Linux: `Arial` is usually not installed. Use `Liberation Sans` instead, or install it: `sudo apt install fonts-liberation`
3. Try increasing `font_size` and `outline` values in `config.yaml` to make captions more prominent

---

### Audio sounds robotic

The fallback TTS providers (Coqui or pyttsx3) are being used instead of the higher-quality cloud providers. Run `python -m autovideo health` to see which providers are reachable.

To improve voice quality:
- Add an `ELEVENLABS_API_KEY` for the best results (free tier available)
- Add Google Cloud TTS credentials for a strong free-tier option
- Install Coqui for a local neural voice: `pip install TTS` (downloads ~1.8 GB model on first use)

---

### The dashboard looks broken or shows garbled text

The live dashboard requires a proper terminal emulator. It does not work in:
- Basic Windows Command Prompt (use Windows Terminal or PowerShell instead)
- Output piped to a file or CI/CD system with no TTY attached

Use `python -m autovideo status` as a non-interactive alternative.

---

### A cloud upload failed but the local video is fine

This is expected behaviour. The local copy in `./output/` always completes first. The cloud failure is logged with the full exception.

To investigate: check the log for the target name (e.g. `s3`, `gdrive`) and the `error_type` field. Common causes: expired credentials, wrong bucket/folder permissions, network timeout. Fix the credentials in `config.yaml` and the next job will upload successfully. You can manually upload the files from `./output/` using your cloud provider's own tools in the meantime.

---

### Import errors or `NameError` after updating files

Stale Python bytecode can cause unexpected errors after updating source files. Clear it:

```bash
# Windows
for /r . %d in (__pycache__) do @if exist "%d" rd /s /q "%d"

# macOS / Linux
find . -name "*.pyc" -delete
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true
```

Then re-run `python validate.py`.

---

## 17. FAQ

**Do I need API keys?**

No. Zero keys are required to produce a complete video. The quality ladder goes: no keys (template script, pyttsx3 voice, placeholder visuals) → Pexels key (real stock footage) → Anthropic key (LLM-written scripts and metadata) → ElevenLabs key (natural voiceover). Add keys in that order for the biggest per-key quality improvement.

---

**How long does one video take?**

On a modern laptop with ElevenLabs and Pexels keys: 30–60 seconds for a 60-second video. Without cloud TTS (pyttsx3 only): 15–25 seconds. The bottleneck is TTS API latency and FFmpeg encoding. With `preset: ultrafast`, encoding takes around 5–10 seconds.

---

**Can I produce videos continuously and unattended?**

Yes. Start the worker pool with `python -m autovideo run`, keep submitting jobs via `batch` or a script that feeds from an RSS feed or topic list, and the system processes them indefinitely. The queue persists across restarts.

---

**Will the same topic ever produce identical videos?**

No. The LLM produces different phrasing each run, stock footage is selected randomly from the top results for each query, background music is randomly selected when multiple tracks match the mood, and the job ID appears in the filename. Even the same topic string produces a distinct video each time.

---

**Does it support languages other than English?**

Yes, fully. Pass `--language ar` for Arabic, `--language hi` for Hindi, `--language fr` for French, and so on. The LLM writes the entire script — title, description, hashtags, and narration — in the target language. ElevenLabs multilingual and Google Cloud TTS both support a wide range of languages. Captions use the ASS Unicode subtitle format which handles right-to-left text.

---

**What happens if the process crashes mid-video?**

Each of the 10 pipeline stages writes a checkpoint before the next stage begins. On restart, the worker reads the checkpoint and skips all completed stages. A crash after TTS but before rendering means TTS is not re-run — the audio file is reused and rendering begins from where it stopped. No API call or encoding pass is duplicated.

---

**How do I clear the queue and start fresh?**

```bash
python -c "
import sqlite3
conn = sqlite3.connect('autovideo.db')
conn.execute('DELETE FROM jobs')
conn.commit()
print('Queue cleared')
conn.close()
"
```

To also clear temporary work files and output:
```bash
# macOS / Linux
rm -rf ./work ./output ./asset_cache

# Windows
rmdir /s /q work output asset_cache
```

---

**Can I use my own pre-recorded voiceover?**

Not directly via the command line. To do it, implement a `LocalFileProvider` that extends `VoiceProvider` and maps narration text to a pre-recorded audio file path, then insert it at the top of the voice chain in `config.yaml`. The interface is documented in [Section 15](#15-adding-a-new-component--developer-reference).

---

**Are the output files compatible with YouTube / Instagram / TikTok?**

Yes. The final encoding pass always produces H.264 video and AAC audio inside an MP4 container with the `+faststart` flag. This is the format explicitly accepted by all three platforms without any additional transcoding or conversion on your part.

---

**How do I make the video play correctly in vertical orientation on phones?**

The default output is 1080×1920 (portrait, 9:16 ratio), which displays correctly in full screen on all short-form platforms and on any phone held vertically. No additional steps are needed.

---

## 18. Glossary

| Term | Meaning in this project |
|---|---|
| **ASS** | Advanced SubStation Alpha — the subtitle format used for karaoke-style word highlighting. Burned directly and permanently into the video file. |
| **Brief** | The internal data structure representing one video job: topic, language, visual mode, narration text. Created from your input by the ingestion stage. |
| **Checkpoint** | A record of which pipeline stages have completed for a given job. Enables crash recovery without repeating completed work. |
| **CRF** | Constant Rate Factor — FFmpeg's quality control setting. Lower = better quality and larger file. Higher = more compression and smaller file. |
| **Dispatcher** | The component that sends finished video files to all configured distribution destinations. |
| **Fallback chain** | An ordered list of providers. If the first fails or is unavailable, the second is tried, and so on. Used for TTS voice synthesis, stock footage, and script generation. |
| **FFmpeg** | The open-source multimedia framework that handles all video encoding, compositing, and format conversion. A required system dependency. |
| **Ingestor** | One of four components (topic, URL, RSS, script) that converts raw user input into a ContentBrief. |
| **Job** | One video production request, tracked from queue entry to delivery. |
| **Ken Burns** | The slow pan-and-zoom effect applied to still images to create the illusion of motion. Named after the documentary filmmaker Ken Burns. |
| **Pipeline** | The 10-stage automated process that transforms a ContentBrief into finished output files. |
| **Placeholder** | A coloured image with the search query text, rendered by Pillow when no stock or AI-generated asset is found. Always available; no API key needed. |
| **pyttsx3** | Python Text-to-Speech library — a wrapper around the operating system's built-in speech engine. The emergency TTS fallback requiring no API key. |
| **Template generator** | The offline script writer used when no Anthropic API key is configured. Produces complete, valid videos with simpler metadata than the LLM version. |
| **TTS** | Text-to-Speech — converting the written narration script into spoken audio. |
| **Visual mode** | The aesthetic look applied to a video (`dark_narration`, `branded`, `cinematic`), controlling saturation, contrast, and vignette. |
| **WAL** | Write-Ahead Logging — the SQLite mode used by the job queue. Ensures jobs are never corrupted if the process is killed mid-write. |
| **Word-accurate timing** | Caption timing to ≤100ms accuracy per word — each word's highlight fires as it is spoken. Provided by ElevenLabs and Google Cloud TTS. |
| **Worker** | A background thread that claims jobs from the queue and runs them through the pipeline. Multiple workers run concurrently. |
