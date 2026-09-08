"""Generate vertical background assets via the Perchance AI image generator.

Perchance (perchance.org/ai-text-to-image-generator) is a free, no-signup,
unlimited text-to-image generator. The generator UI lives in a nested iframe, and
each rendered result is painted into further-nested embed iframes as an inline
"data:image/jpeg;base64,..." blob. We drive the real Edge profile:

  1. locate the generator iframe (the one holding the visible description box),
  2. fill the prompt and click the generate button,
  3. wait for new finished images to appear,
  4. capture the base64 blobs and save them to disk.

Prompt/image correlation (R2-W5): results are correlated back to the prompt we
just submitted via the *DOM container* that owns the newest generation, not a
page-wide content-hash diff. Each generation is painted into its own embed frame
underneath the generator frame; DOM order therefore == generation order, so after
submitting prompt N we read images only from the last embed frame that gained new
images. A whole page-wide scan remains as a last-resort fallback if the scoped
frames never appear (markup change), never the primary path.

No REST API, no paywall — this is the web UI, scripted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from itertools import cycle
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ... import config

log = logging.getLogger(__name__)

GENERATOR_URL = "https://perchance.org/ai-text-to-image-generator"

# The visible description/prompt box (the plain "textarea" matches a hidden
# scratchpad, so we target by data-name).
PROMPT_SELECTOR = "textarea[data-name='description']"
# The generate button (rendered as an emoji + 'generate' text).
GENERATE_SELECTOR = "button:has-text('generate')"
# Finished images are inline data-URIs (there are no remote src results).
IMG_SELECTOR = "img[src^='data:image']"
# R6 native generator controls: Shape (768x768 Square / 512x768 Portrait /
# 768x512 Landscape) and "How many" (batches that many variants per prompt).
SHAPE_SELECTOR = "select[data-name='shape']"
NUM_IMAGES_SELECTOR = "select[data-name='numImages']"

# Ordered frame -> [(image hash, base64 payload)] captured beneath the generator
# frame. Keys must be order-significant so "last frame with new images" works.
FrameSnap = List[Tuple[str, List[Tuple[str, str]]]]


def _generator_frame(page):
    """Return the nested iframe holding the generator's description box."""
    for f in page.frames:
        if f is page.main_frame:
            continue
        try:
            if f.locator(PROMPT_SELECTOR).count() > 0:
                return f
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("Could not locate the Perchance generator iframe")


def _hash_of(b64: str) -> str:
    return hashlib.sha256(b64.encode()).hexdigest()[:16]


def _should_downgrade_scoped(seen_scoped_this_prompt: bool, elapsed_s: float,
                             in_flight_s: float) -> bool:
    """Whether an empty scoped read means the site shifted (downgrade) versus
    just an in-flight generation (keep polling scoped). R5: the generator
    clears its canvas while rendering, so an empty read inside the normal
    in-flight window must NOT count as a markup change."""
    return seen_scoped_this_prompt or elapsed_s >= in_flight_s


def _per_prompt_target(remaining: int, batch: int) -> int:
    """How many finished variants one prompt should supply at most."""
    return min(max(int(batch), 1), remaining)


def _apply_generator_settings(gen) -> None:
    """Switches the generator to the configured Shape/"How many" (R6).

    ``select_option`` fires the native change event, which the plugin persists
    for subsequent generates. Failures are non-fatal (site defaults remain).
    """
    shape = (config.PERCHANCE_SHAPE or "").strip()
    if shape:
        try:
            loc = gen.locator(SHAPE_SELECTOR)
            if loc.count() > 0:
                loc.select_option(shape)
                log.info("Perchance shape set to %r", shape)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not set Perchance shape=%r (%s)", shape, exc)
    num_images = int(config.PERCHANCE_NUM_IMAGES or 0)
    if num_images > 0:
        try:
            loc = gen.locator(NUM_IMAGES_SELECTOR)
            if loc.count() > 0:
                loc.select_option(str(num_images))
                log.info("Perchance numImages set to %d", num_images)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not set Perchance numImages=%d (%s)",
                        num_images, exc)


def _collect_snapshot(page, gen) -> FrameSnap:
    """Snapshot images **only in embed frames under the generator frame**, in DOM
    order. Each generation owns its embed frame, so this is the container-scoped
    view the correlation uses (R2-W5).

    Returns an ordered list of ``(frame_key, [(hash, b64), ...])``. Frame keys
    are ``<index>:<url>``; index == position among the generator frame's children
    == generation order.
    """
    scoped: List = []
    for f in page.frames:
        if f is page.main_frame or f is gen or f.parent_frame is not gen:
            continue
        try:
            imgs = f.locator(IMG_SELECTOR)
            n = imgs.count()
        except Exception:  # noqa: BLE001
            continue
        if n == 0:
            continue
        items: List[Tuple[str, str]] = []
        for i in range(n):
            try:
                src = imgs.nth(i).get_attribute("src") or ""
            except Exception:  # noqa: BLE001
                continue
            if not src.startswith("data:image"):
                continue
            b64 = src.split(",", 1)[1] if "," in src else ""
            if not b64:
                continue
            items.append((_hash_of(b64), b64))
        if items:
            scoped.append((f"{len(scoped)}:{f.url}", items))
    return scoped


def _collect_finished_images(page):
    """Legacy page-wide sweep — used only as a fallback when no generation-scoped
    frames can be found (markup change). Kept so a UI update never silently
    regresses asset capture."""
    results: Dict[str, str] = {}
    for f in page.frames:
        try:
            imgs = f.locator(IMG_SELECTOR)
            n = imgs.count()
            for i in range(n):
                src = imgs.nth(i).get_attribute("src") or ""
                if not src.startswith("data:image"):
                    continue
                b64 = src.split(",", 1)[1] if "," in src else ""
                if not b64:
                    continue
                results[_hash_of(b64)] = b64
        except Exception:  # noqa: BLE001
            continue
    return results


def _pick_newest_correlated(prev: FrameSnap, cur: FrameSnap) -> Tuple[List[str], Optional[str]]:
    """Given ``before`` and ``after`` generation-scoped snapshots, return the
    ``(new image hashes, chosen frame key)`` belonging to the generation we just
    submitted.

    The newest generation is the last embed frame (DOM/generation order) that
    gained images since the previous poll. Within that frame all newly finished
    variants are returned in DOM order (deterministic, "the specific generation's
    DOM container"), instead of an arbitrary member of a page-wide diff.
    """
    prev_by_key = {k: {h for h, _ in items} for k, items in prev}
    gained: List[Tuple[int, str, List[str]]] = []
    for i, (key, items) in enumerate(cur):
        new_hashes = [h for h, _ in items if h not in prev_by_key.get(key, set())]
        if new_hashes:
            gained.append((i, key, new_hashes))
    if not gained:
        return [], None
    _, chosen_key, hashes = gained[-1]
    return hashes, chosen_key


def run(ctx, inputs, run_dir, session):
    script_path = Path(inputs["script"])
    script = json.loads(script_path.read_text(encoding="utf-8"))
    image_count = int(inputs.get("image_count", 6))
    prompts = script.get("image_prompts", []) or []
    if not prompts:
        raise RuntimeError("Script has no image prompts for Perchance assets")

    page = session.first_page()
    from ...resilience.interaction import ElementInteractor

    ux = ElementInteractor(page, provider="perchance", settings=ctx.settings)
    ux.goto(GENERATOR_URL, wait_until="domcontentloaded")
    time.sleep(config.PERCHANCE_SETTLE_S)  # let the generator iframe + UI finish loading
    _generator_frame(page)
    _apply_generator_settings(_generator_frame(page))

    assets_dir = run_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    saved: list = []
    batch = int(config.PERCHANCE_NUM_IMAGES or 1)
    remaining = image_count
    prompt_iter = cycle(prompts)

    while remaining > 0:
        prompt = next(prompt_iter)
        target = _per_prompt_target(remaining, batch)
        gen = _generator_frame(page)
        before = _collect_snapshot(page, gen)
        scoped_ok = bool(before)

        box = gen.locator(PROMPT_SELECTOR).first
        box.click(timeout=15000)
        box.fill("", timeout=8000)
        box.type(prompt, delay=6)

        generate = gen.locator(GENERATE_SELECTOR).first
        ux.click(generate, description="perchance generate")

        # wait for the specific generation's container (embed frame) to turn up
        # new images; scoped correlation first, page-wide diff only as fallback.
        # The generator clears its canvas as soon as a new prompt starts, so the
        # scoped collection legitimately reads EMPTY for the whole ~30s the next
        # image is rendering. Treating any empty read as "markup shifted" caused
        # every prompt to bail to the page-wide fallback instantly (R5). Only
        # downgrade once the scoped view has stayed empty well past the normal
        # in-flight window (or previously held images and then lost them).
        # R6 batch: keep polling until this prompt's variant target is met —
        # a single poll can legitimately yield more than one finished image.
        collected: Dict[str, str] = {}
        t0 = time.time()
        in_flight_s = config.PERCHANCE_MAX_PER_IMAGE_S // 2
        seen_scoped_this_prompt = False
        while time.time() - t0 < config.PERCHANCE_MAX_PER_IMAGE_S:
            time.sleep(config.PERCHANCE_POLL_INTERVAL_S)
            if scoped_ok:
                cur = _collect_snapshot(page, gen)
                if not cur:
                    if not _should_downgrade_scoped(
                            seen_scoped_this_prompt, time.time() - t0, in_flight_s):
                        # plain in-flight window; keep polling the scoped view
                        continue
                    log.warning("Generation-scoped image collection vanished "
                                "mid-run; falling back to page-wide diff")
                    scoped_ok = False
                    before = _collect_finished_images(page)
                    continue
                seen_scoped_this_prompt = True
                new_hashes, _key = _pick_newest_correlated(before, cur)
                if new_hashes:
                    by_hash = {h: b64 for _k2, items in cur for h, b64 in items}
                    collected.update({h: by_hash[h] for h in new_hashes
                                      if h in by_hash})
                    if len(collected) >= target:
                        break
                before = cur
            else:
                cur = _collect_finished_images(page)
                new_keys = [k for k in cur if k not in before]
                if new_keys:
                    collected.update({k: cur[k] for k in new_keys})
                    if len(collected) >= target:
                        break
                before = set(cur)

        if not collected:
            log.warning("No finished image captured for prompt %r; "
                        "moving to next prompt", prompt)
            continue

        # save every new variant of THIS prompt (batch mode), up to what's left
        for b64 in collected.values():
            if remaining <= 0:
                break
            data = _decode(b64, len(saved))
            if data is None:
                continue
            fname = assets_dir / f"bg_{len(saved):02d}.jpg"
            fname.write_bytes(data)
            saved.append(str(fname))
            remaining -= 1
            log.info("Saved asset %d -> %s (%d bytes)", len(saved),
                     fname.name, len(data))
        time.sleep(config.PERCHANCE_UI_SETTLE_S)

    if not saved:
        raise RuntimeError("Perchance produced no saved images")

    return {"images": str(assets_dir), "image_files": saved}


def _decode(b64: str, idx: int) -> Optional[bytes]:
    try:
        return base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not decode image for prompt %d: %s", idx, exc)
        return None
