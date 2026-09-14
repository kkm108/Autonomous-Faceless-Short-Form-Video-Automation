# Tasks-R10: Content Quality & Authenticity — Instructions for the Agent

**Why this file is different from R1–R9:** every prior round hardened *whether the pipeline runs correctly*. This one is about *whether what it produces deserves to be watched and trusted*. Those are genuinely different problems, and passing every existing test and quality gate does not mean this bar is met — the last review proved that by finding real defects (image/caption mismatch, clipped text) in a video that had already passed every automated check this project has built.

**The one idea underneath everything below:** this system is about to run, largely unattended, across 45 channels — several of which give people health, financial, and security information. No amount of retry logic or idempotency makes a wrong fact true. That risk category doesn't get smaller with more automation; it needs a different kind of answer.

---

## Priority 0 — Fix before another video ships (from the direct video review)

- [ ] **Image reuse/mismatch bug.** At least two slides in the reviewed video displayed an image belonging to a different, earlier slide instead of art matching their own caption. Find out whether Perchance actually generated 6 distinct images for that run or silently fell back/reused one, and whether the existing quality gate should have caught this as degraded output (it currently checks image *count*, not whether each image is actually distinct/relevant) and force-downgraded to unlisted instead of letting it pass silently.
- [ ] **Caption clipping bug.** At least one slide's caption text was cut off at the frame edge. Reproduce it with the exact triggering text and fix the wrap/centering calculation — this is a rendering defect, not a style choice.
- [ ] **Don't schedule anything until both are confirmed fixed by actually watching a freshly generated video**, not by re-running the test suite. The test suite didn't catch either of these; a person watching the output did.

## Priority 1 — Tier the channels by what happens if the content is wrong

Not all 45 channels carry the same risk when the automation gets something wrong. Add a `risk_tier` field to each entry in `channels.yaml` and treat the two tiers differently everywhere downstream (fact-checking, rollout pace, review cadence):

- [ ] **`high` — giving advice or factual claims where being wrong causes real harm:** `en-finance-v2`, `hi-finance`, `es-finance`, `en-investing`, `en-crypto`, `en-health`, `hi-health`, `en-nutrition`, `en-fitness`, `hi-fitness`, `en-sleep`, `en-cybersecurity`, `en-real-estate`, `en-psychology`, `hi-psychology`, `ai-hi-business`, `en-business-v2`, `en-economics`, `en-career`, `en-leadership`, `en-productivity`.
- [ ] **`low` — trivia, entertainment, or subjective/cultural content where a mistake is embarrassing, not harmful:** `en-history-v2`, `hi-history`, `en-mysteries`, `en-science-facts`, `en-automotive`, `en-gaming-v2`, `en-travel`, `en-fashion`, `en-space`, `en-mathematics-v2`, `en-physics`, `en-biology`, `en-language`, `en-motivation`, `hi-motivation`, `en-spirituality`, `hi-spirituality`, `en-stoicism`, `en-philosophy`, `en-parenting`, `en-environment`, `ai-en-marketing`, `en-ai-skills`, `en-mathematics-v2`, `hi-science`.

This tiering is the thing everything else in this file keys off. It's a genuine judgment call, not a mechanical fact — revisit it as the channels' actual content becomes clearer.

## Priority 2 — Give the pipeline a way to catch its own wrong claims

There is currently no step anywhere that checks whether a generated script's factual claims are actually correct. For `high`-tier channels specifically:

- [ ] Add a second LLM pass — after script generation, before assets — that reviews the script specifically for confident-sounding claims and flags anything uncertain, a specific number/statistic that should be double-checked, or advice stated more definitively than the source material would support. This doesn't need to be perfect; it needs to catch the "sounds authoritative, is actually a guess" pattern the underlying models are prone to.
- [ ] Anything flagged should force the same soft-fail path that already exists for degraded images (downgrade to unlisted, don't hard-block) — a human reviews before it goes public, rather than the run failing outright.
- [ ] For `high`-tier channels, add a genuine, honest disclaimer in the video description — not for legal cover, but because "for general informational purposes, not professional advice" is simply true of AI-generated financial/health/security content and viewers deserve to know that.
- [ ] `low`-tier channels don't need this pass — the cost of a wrong "accidental invention" fact is embarrassment, not harm. Don't slow down entertainment content with a check that exists to catch a different kind of risk.

## Priority 3 — Make "passed the quality gate" and "is actually good" the same thing

The existing quality gates check mechanical properties: does the file exist, does it have the right duration, are there enough images. They don't check whether the images are *right*. Close that gap rather than adding more mechanical checks:

- [ ] Institute an actual human-watch sampling practice, not just automated gates: every video for the first two weeks of any new channel, then a regular sample (e.g., 1 in 10) once a channel's output has been trustworthy for a while — weighted more heavily toward `high`-tier channels. This is not a step that can be fully automated away; it's the thing that caught both P0 bugs when nothing else did.
- [ ] Build a fast per-video self-check into the assembly stage itself where it's feasible mechanically: does each generated image's content-hash differ from every other image in the same video (catches the exact reuse bug found); does rendered caption text actually fit within the frame's safe margins before compositing (catches the exact clipping bug found). These are cheap, mechanical, and would have caught both bugs automatically — add them, but don't treat them as a substitute for actually watching output occasionally.

## Priority 4 — Don't let scale outrun quality

The "Accepted trade-offs" section already names the content-policy risk of looking mass-produced. At 45 channels, that risk is compounded by *actual* quality gaps, not just perceived repetition:

- [ ] Don't activate all 45 channels at once. Roll out in tiers: a handful of `low`-tier channels first (lower consequence if something's still rough), confirm quality holds over real output, then `high`-tier channels one or two at a time with closer review, given they're the ones where a mistake matters more.
- [ ] Make each channel's R8 branding profile (tone, accent color, font) genuinely distinctive rather than a single adjective in a prompt — the goal is that a viewer scrolling between two of these channels shouldn't be able to tell they share a pipeline. That's not cosmetic; it's a real, direct answer to the "looks mass-produced" risk.
- [ ] Revisit this after the first real batch of multi-channel output exists — the right cadence and tiering will be clearer with actual evidence than it can be from planning alone.

---

## What this file is deliberately not asking for

More retry logic, more idempotency guards, more fallback chains. That work has been done well and this project's reliability is genuinely solid at this point. The gap left isn't "the pipeline breaks" — it's "the pipeline can produce something wrong or low-quality without anything noticing," which is a content problem, not an engineering one. Some of what closes that gap is code (Priority 2's fact-check pass, Priority 3's mechanical checks). Some of it genuinely isn't — Priority 3's human-watch practice and Priority 4's rollout pacing are judgment calls that need a person paying attention, on a recurring basis, not a one-time setup task to hand to an agent and consider done.
