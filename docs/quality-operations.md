# Content Quality & Authenticity — Operating Practice (R10)

Status: internal operating guide for humans. The mechanical gates are code;
what's in this file is the judgment and cadence that code cannot replace.

Context: R10 exists because a human watching a video that had passed every
automated check found two real defects (a reused/mismatched image and clipped
caption text). The pipeline now catches both mechanically (below). This file is
the recurring human practice that sits on top of those checks — it is the thing
that found the P0 bugs, and it is not a one-time setup.

## What the code now catches automatically

Both P0 defects now produce machine-verifiable evidence in the assembly stage,
written to `caption_fit.json` in each run's output directory:

- **Image distinctness** — SHA-256 content hash of every generated image; any
  byte-identical duplicate within one video fails the run. The asset quality
  gate also hashes files and soft-fails (downgrades to unlisted) on duplicates.
- **Caption fit** — before compositing, each rendered caption line is measured
  against the frame's safe margins; over-long tokens are hard-broken and the
  font shrinks through a fixed ladder until the block fits. `all_fit` plus a
  per-slide record prove nothing clipped.

These contradict the claim that "passed" and "good" are the same thing. Use the
sample schedule below so a human actually looks at output on a recurring basis.

## Working definition: what "good" means when sampling

Watch not for crashes (gates handle that) but for *content*:

- The image on each slide actually matches that slide's caption (the P0 bug).
- Caption text fits inside safe margins on every slide (the P0 bug).
- Claimed facts/statistics are plausible and match the source the script
  cites; nothing sounds authoritative-because-the-model-committed, especially
  numbers and advice on high-tier channels.
- Voiceover, captions, and slides are in sync; nothing is repeated or skipped.
- The piece is on-tone for its channel and doesn't feel like another channel's
  work (see Branding distinctiveness, below).

Flag anything that fails the first three as a P0: fix before another video
ships on that channel.

## Human-watch sampling cadence (P3)

- **New channel:** every video for the first two weeks, regardless of tier.
  That's ~14 watches per new channel before any trust is assumed.
- **Steady state:** once a channel's output has been trustworthy for a while,
  sample regularly at ~1 in 10 videos (`low` tier) and ~2–3 in 10 (`high` tier).
  The ratio is deliberately weighted toward high-tier: a wrong DIY-health fact
  is harm, a wrong accidental-invention fact is embarrassment.
- **After any pipeline change**, resume every-video sampling on every active
  channel for the first few outputs — the P0 bugs shipped because no code
  touched those paths; the change itself is the risk.

Make it easy to audit: the same `output\<run_id>\caption_fit.json` proves the
mechanical checks ran. Sampling is watching the finished video (and thumbnail),
not re-reading gate output.

## Rollout pacing (P4)

Do not activate all 45 channels at once.

1. **Wave 1 — a handful of `low`-tier channels** (lower consequence if
   something is still rough). Confirm quality holds over real output, watched
   per the cadence above.
2. **Wave 2 — `high`-tier channels one or two at a time**, closer review and
   denser sampling: this is where the fact-check pass, the disclaimer, and the
   human eye all matter at once.
3. Re-check actual production volume against the operations throughput table
   (`docs/operations.md`) before adding channels; daily cadence is an
   account-health metric, not just a queue depth.

`risk_tier` produces the behavior differences that make this rollout possible
(fact-check + description disclaimer for high tier, no bottleneck for low).

## Branding distinctiveness (P4)

A viewer scrolling between two of these channels shouldn't be able to tell
they share a pipeline. Each channel's R8 branding profile (tone, accent color,
preferred font, logo) now also carries a `visual_style` phrase that feeds the
image-generation prompt, so the art matches the channel identity, not just the
caption. When adding a channel, give it a `visual_style` that is genuinely its
own — not a reworded copy of a sibling's. This is a direct mitigation of the
"mass-produced" signal, not cosmetics.

## Revisit marks

- Tiering: the `high`/`low` split keys off the whole operating model yet is a
  judgment call. Revisit as real channel content makes itself clearer.
- Cadence: the 2-week/1-in-10 numbers are starting points. If a channel
  repeatedly trips the P0-watch bar, tighten its sampling; if a channel has a
  long clean streak, the sample ratio can loosen.
- Rollout: revisit wave order after the first real batch of multi-channel
  output exists — actual evidence beats planning assumptions here.

## Housekeeping log
- 2026-09-14: created; P0 checks live in the assembly stage
  (`caption_fit.json`: `all_fit`, `image_hashes_distinct`) and the asset
  quality gate (hash-based distinctness soft-fail).