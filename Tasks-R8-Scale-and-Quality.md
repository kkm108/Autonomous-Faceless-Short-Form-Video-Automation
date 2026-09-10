# Tasks-R8: Multi-Channel Scale-Out & Production-Quality Upgrade

**Context:** R1–R7 hardened and validated the pipeline for a single channel. This round is different in kind: it's new capability, not bug-fixing, triggered by two things — the channel count going from ~12 to 45 (across English, Hindi, and Spanish, with more planned), and a request to bring video production up to a real "ready to publish anywhere" standard (fallback images, synced captions, music/SFX, per-channel branding, subtitles, thumbnails, richer metadata).

**How to use this file:** Part A and Part B are largely independent of each other and can be worked in parallel. Within each part, respect the phase order — later items consume artifacts (a channel registry, real caption timing) that earlier items produce. Don't start Phase 3 before Phase 1 is real, or you'll be building per-channel branding against a registry that doesn't exist yet.

---

## Part A — Multi-channel consistency at scale

### Phase 1 (foundational)

- [ ] **A1 — Replace the hardcoded channel map with an external, structured registry.** `config.py`'s current dict-literal approach doesn't scale to 45 entries, let alone "more as needs grow." Move to a data file (YAML is more readable than JSON for something humans will hand-edit regularly; the codebase already parses JSON elsewhere if you'd rather stay consistent) with one entry per channel:
  ```yaml
  en-history-v2:
    channel_id: "UC..."           # validated, 24-char
    language: en
    keywords: [history, ancient, empire, archaeology]
    branding:
      logo: assets/brand/en-history-v2/logo.png
      accent_color: "#8a6d3b"
      font: assets/brand/en-history-v2/font.ttf
      tone: "calm, curious, slightly reverent — like a museum guide, not a hype channel"
  ```
  Adding channel #46 becomes a data edit, not a code change. `channels.py`'s resolution logic stays the same shape, it just reads from this file instead of a Python dict.

- [ ] **A2 — Unify channel, language, and TTS routing behind one lookup.** Right now `channels.py` (topic → channel) and `tts/routing.py` (language → TTS provider) are two independent decisions that can silently disagree — e.g. resolving to a Hindi channel while the script gets generated in English. Fix: the registry entry's `language` field (from A1) becomes the single source of truth driving three downstream decisions from one lookup — which channel to publish to, what language the scripting prompt requests, and which TTS voice route to use. Not three independently-inferred signals; one fact with three consumers.

### Phase 2 (independent of Phase 1, foundational for Part B)

- [ ] **B2 — Real caption sync, not equal division.** `ffmpeg.py`'s `segment = duration / len(slide_paths)` was a known simplification from the original review, never revisited. Fix depends on which TTS provider actually served the run: `edge_tts` exposes real word-boundary timestamps natively — capture and use them directly when it's the active provider. For providers that don't expose word timing (the browser-based SoundTools path, `pyttsx3`), either document the coarser per-slide timing honestly as a fallback, or add a lightweight forced-alignment pass against the generated audio. Don't build one timing path and quietly apply it everywhere as if it were equally precise regardless of provider — it won't be.

### Phase 3 (consumes A1's registry)

- [ ] **A3 — Per-channel branding & tone profile feeds both script generation and assembly.** The registry's `tone` string gets injected into the LLM system prompt so a viewer scrolling between videos on the *same* channel gets a consistent voice — right now every channel gets whatever the model felt like that run. The `accent_color`/`font` drive caption and thumbnail styling (B7) so a channel looks like itself across videos, not like whichever default the renderer shipped with.
- [ ] **B5 — Per-channel watermark/logo overlay.** A straightforward ffmpeg overlay (corner-positioned, semi-transparent, consistent placement) once A1 gives each channel a real logo asset path. Low complexity, direct payoff for brand consistency.
- [ ] **B7 — Custom thumbnails, not YouTube's auto-generated one.** Reuse the best (or simply the first) Perchance background image, composite a bold title card using the *same* PIL text-rendering machinery already built for burned-in captions (`_render_slide` / `_draw_text_wrapped` in `ffmpeg.py` — this is mostly reuse, not new infrastructure), styled per A3's channel branding. Upload it via a new Studio interaction alongside the existing publish flow.

### Phase 4 (consumes B2's timing data)

- [ ] **B6 — Subtitle sidecar files (SRT/VTT).** Once B2 produces real per-word or per-phrase timing, serializing it into a standard SRT/VTT file is comparatively cheap — and it pays off twice: once as a sidecar artifact, and once as an actual YouTube caption track you can upload (better accessibility and SEO than relying on YouTube's own auto-captions, and a second, independent transcript in case the burned-in captions are ever wrong).

### Phase 5 (independent, can run in parallel with 1–4)

- [ ] **B1 — Fallback image-provider chain.** Apply the same pattern already proven *twice* in this codebase — TTS (soundtools → edge_tts → pyttsx3) and scripting (ai_studio → duckai → ask_brave → gemini → chatgpt) — to image generation: Perchance stays primary, with one or two no-login/free alternatives behind a shared interface so the caller doesn't need to know which provider actually served a given image. **Scope identifying and vetting the actual candidate sites as its own research pass first** — the same kind of verification that went into the no-login chat list (confirmed no-login, confirmed reliable, confirmed license-clean for the generated images) — rather than wiring up a fallback chain around sites nobody's checked.
- [ ] **B3 — Background music with ducking.** Needs a small, properly-licensed royalty-free track library — bundle a curated set or source from somewhere with genuinely clear reuse terms, verified the same way, not just "whatever's convenient." Mix it in via a real ducking approach (lower music under narration — a sidechain-compression or scripted volume-envelope filter in ffmpeg) rather than a flat overlay that competes with the voice.

### Phase 6 (process/ops, non-blocking, but genuinely worth doing before this scales further)

- [ ] **A4 — Deterministic collision handling.** With 45 keyword sets, some topics will legitimately match more than one channel (e.g. "AI's impact on employment" could hit `en-ai-skills`, `en-economics`, and `en-career`). Define an explicit, documented tie-break (first-listed-wins, or a specificity score based on distinct keyword hits) rather than leaving it to whatever order a dict happens to resolve in.
- [ ] **A5 — Preview/dry-run mode.** Given the stakes now span 45 distinct public brand identities, add a mode that resolves and *prints* a planned topic → channel → language assignment for a batch without executing anything, so a human can sanity-check a day's or week's worth of planned assignments before any of it goes live. `channels.py`'s resolution is already a pure function — this is mostly a thin CLI wrapper around it.
- [ ] **A6 — Face the operational math now, not after it's a problem.** Each run currently takes on the order of 10+ minutes end to end (script, assets, voiceover, assembly, publish, plus any rate-limit waits — consistent with the timings already reported across this project's live runs). At 45 channels, even one video per channel per day is 45 sequential runs on one machine driving one browser — a multi-hour daily commitment. Decide deliberately whether the near-term goal is genuinely "all 45 channels active daily" (in which case this needs a scheduling/queueing layer and a realistic per-day channel budget) or "a handful running, growing gradually" (in which case this is a non-issue for now) — rather than letting the channel *count* quietly outgrow what the pipeline can actually execute in a day.
- [ ] **A7 — Re-flag the content-policy trade-off at 4x the original scale.** This was accepted early in this project at a much smaller scale (1 channel). At 40+ channels run through the identical pipeline, the risk profile changes: near-identical production values across dozens of channels is a much more legible "mass-produced" signal to a platform that actively watches for that pattern. A3's per-channel branding is a direct, genuine mitigation here, not just cosmetics — and it's worth monitoring each channel's own monetization/distribution health as more come online, rather than assuming what worked for channel 1 safely generalizes to channel 45.

### Phase 7 (polish, sequence last)

- [ ] **B4 — SFX.** Lower priority than music/captions/thumbnails — needs a triggering mechanism (e.g., keyed to slide transitions) before it's worth building at all. Don't build this before B2/B3 land.
- [ ] **B8 — Rich per-video metadata.** The original review flagged that every upload gets the identical generic description ("Automated faceless short.\n\n#shorts") regardless of topic — this is what "sidecar etc." most likely pointed at beyond subtitles. Bundle into one pass: a real description drawn from the actual script content, relevant tags/keywords, and chapter markers where the video's structure supports them, rather than leaving this as a vague catch-all.

---

## Known current-video weaknesses worth confirming or expanding

You mentioned "several weaknesses" without specifics — these are the ones already known from this codebase's history and likely what's being felt, but worth confirming against what you're actually seeing:
- Captions divide time equally per slide regardless of how long each line actually takes to say (→ B2).
- Every video uses whatever Perchance's default art style happens to be ("Casual Photo" per the earlier screenshot) regardless of topic or channel tone — no connection to A3's planned per-channel tone/style guidance yet.
- No audio normalization across TTS providers — since the pipeline can fall through soundtools → edge_tts → pyttsx3 mid-run, volume/quality can vary run to run depending on which one actually served a given video.
- The single flat Ken-Burns zoompan effect is applied identically to every slide on every video — no per-channel or per-mood visual variation.
- Generic, identical description/tags on every upload (→ B8).

If there's something else specifically bothering you about the current output, worth naming it directly — a vague "quality" target is hard to build against, and the list above is where the pipeline is already known to be thin rather than a description of what you're actually seeing.
