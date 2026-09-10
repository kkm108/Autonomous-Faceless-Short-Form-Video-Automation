# Operations & Policy Notes (R8 internal)

Status: implementation notes for operators; not user-facing documentation.

## Throughput math (A6) — measured vs estimated

Measured locally on this machine (R8 session):

| Stage | Time | Source |
|---|---|---|
| Encode 35 s 1080x1920@30, libx264 veryfast | ~6 s | microbenchmark (5.6 s) |
| Full local assembly (images + voiceover + music + SFX + subtitles + thumbnail) | < 20 s | synthetic e2e run |

Estimated per full run, dominated by network/browser-bound stages (from R7
live runs: idea sourcing via ask_studio with fallbacks, Perchance image
fetches, headful Studio publish):

| Stage | Estimate |
|---|---|
| Ideation (ask_studio→fallbacks) | 1–3 min (rate-limit waits possible) |
| Script LLM | 10–30 s |
| 6 poster images (Perchance primary; keyless fallback faster) | 1–2 min |
| TTS (edge_tts network; pyttsx3 local) | 5–20 s |
| Assembly (measured) | < 1 min |
| Publish (headful browser) | 1–3 min incl. verification |

**End-to-end ≈ 4–12 min/video sequential.** At 45 channels × 1/day that is a
~3–9 h single-machine, single-browser commitment — over a nightly window that
is marginal. Two viable postures:

1. **Elite few + grow gradually** (recommended start): a hand-managed set of
   3–10 channels at 1/day → ~30–120 min nightly. Headroom to parallelize.
2. **All 45 daily**: needs a queue/scheduler, per-channel budget caps, staged
   ramp (3→5→10→20/day per channel is far safer for a new channel), and
   spreading runs across the day rather than one nightly burst.

Storage is not a constraint (final shorts are low single-digit MB). The real
constraints are (a) serial wall-clock, (b) platform upload-rate limits on new
channels, and (c) detection risk (below).

## Scale-up policy trade-offs (A7)

- **Mass-produced signal.** 40+ channels with identical automated pipeline and
  near-identical production values is a legible "mass-produced" pattern, and a
  much bigger target than 1 channel. A3 per-channel branding (tone, accent
  color, font, logo) is the direct mitigation — use it, don't skip it. Monitor
  each channel's distribution/monetization health individually; do not assume
  channel 1's behavior generalizes to channel 45.
- **First-listed-wins ties.** With 45 keyword sets, ambiguous topics resolve by
  registry order (A4) — deterministic, but worth reviewing periodically so a
  popular keyword doesn't silently route everything to one channel.
- **Degraded-run handling.** If image rescues kick in (Perchance falls short),
  the run publishes unlisted rather than an off-brand abortion, and the
  operator is expected to review/list it manually. This is a deliberate
  failure mode, not a silent success.
- **License hygiene.** All music/SFX must be genuinely reusable (see
  `assets/music/README.md`); the engine does not validate licenses.
- **Rate limiting.** New YouTube channels uploading many Shorts/day risk
  throttling or review. Ramp daily volume gradually. Each run is a live user
  action on a real account — treat upload counts as an account-health metric.

## Laid to rest in R8 (known weaknesses)

- Equal-division caption timing → real edge_tts word-boundary timing, honest
  estimated fallback (B2).
- Generic identical metadata → per-video description/tags/chapters + thumbnail
  (B7/B8).
- One flat zoompan → per-slide Ken-Burns segments (assembly refactor within B7
  phase work).
- Zero/none music → ducked music bed, slide SFX (B3/B4).
- Missing images → keyless fallback chain, degraded-run downgrade (B1).
- Hardcoded channel map → `channels.yaml` registry, validated 24-char ids (A1).

## Pending user decisions

1. R8 milestone: commit the current state? (R7 changes remain uncommitted.)
2. Scheduled/queued run layer: build it now (all-45 posture) or validate a
   single live R8 run first?
3. Backfill orphaned unlisted shorts from the 09-07/09-08 test runs?
4. Clean up stale `output\1788953848_f4805d`?

## Orphaned unlisted shorts — queued for review (2026-09-10 sweep)

These are unlisted test uploads left behind by the 09-07→09-09 validation runs.
They were never intended to be public, and YouTube URLs extracted at the time
were unreliable (a pre-R7 extraction bug — some `watch?v=` values repeat or are
blank). The titles + run dirs below are the reliable record. Decide per short:
**delete**, **list**, or leave **unlisted**. A bulk-delete pass via Studio is
possible later (browser-driven, same caution as publishing).

| Date (run) | Title | Run dir |
|---|---|---|
| 09-07 11:37 | Why Ancient Rome Built Straight Roads | `output\1788781078_8adcba` |
| 09-08 12:43 | 3 Life-Changing Benefits of Morning Sunlight | `output\1788871430_b630d3` |
| 09-08 12:54 | 5 Proven Daily Habits of Successful People | `output\1788872042_c86a2a` |
| 09-09 05:44 | Why Birds Sing at Dawn | `output\1788932674_193c85` |
| 09-09 06:24 | The Shocking Story Behind Your Pencil | `output\1788935086_b6ba2a` |
| 09-09 06:43 | How Compasses Actually Work | `output\1788936204_292ff2` |
| 09-09 07:02 | The Hidden Reason Why Your Brain Makes You Yawn | `output\1788937336_a73fd8` |
| 09-09 07:20 | How Bees Actually Make Liquid Gold | `output\1788938450_6f2ca7` |
| 09-09 07:38 | The Hidden Truth Behind Autumn Leaves | `output\1788939488_4084e7` |
| 09-09 09:29 | 5 High Protein Meal Prep Ideas For Your Week | `output\1788946195_127364` |
| 09-09 11:38 | Find The Hidden Number Before Time Runs Out | `output\1788953936_c2ee7b` |

(R7's own publish `output\1789018170_530090` → `https://www.youtube.com/watch?v=chehvTEANzM` is intentional, not orphaned.)

## Housekeeping log
- 2026-09-10: removed stale `output\1788953848_f4805d`.
- 2026-09-10: R7 + R8 committed as `c707971`.

## Resolved decisions
- 2026-09-10: R8 posture = validate one live R8 run first.