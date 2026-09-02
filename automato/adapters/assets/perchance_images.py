"""Generate vertical background assets via the Perchance AI image generator.

Perchance (perchance.org/ai-text-to-image-generator) is a free, no-signup,
unlimited text-to-image generator. The generator UI lives in a nested iframe, and
each rendered result is painted into further-nested embed iframes as an inline
"data:image/jpeg;base64,..." blob. We drive the real Edge profile:

  1. locate the generator iframe (the one holding the visible description box),
  2. fill the prompt and click the generate button,
  3. wait for new finished images to appear,
  4. capture the base64 blobs and save them to disk.

No REST API, no paywall — this is the web UI, scripted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

GENERATOR_URL = "https://perchance.org/ai-text-to-image-generator"

# The visible description/prompt box (the plain "textarea" matches a hidden
# scratchpad, so we target by data-name).
PROMPT_SELECTOR = "textarea[data-name='description']"
# The generate button (rendered as an emoji + 'generate' text).
GENERATE_SELECTOR = "button:has-text('generate')"

MAX_PER_IMAGE_SEC = 150
POLL_INTERVAL_SEC = 5


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


def _collect_finished_images(page):
    """Gather finished embed images as {content_hash: base64_bytes}."""
    results: dict = {}
    for f in page.frames:
        try:
            imgs = f.locator("img[src^='data:image']")
            n = imgs.count()
            for i in range(n):
                src = imgs.nth(i).get_attribute("src") or ""
                if not src.startswith("data:image"):
                    continue
                b64 = src.split(",", 1)[1] if "," in src else ""
                if not b64:
                    continue
                key = hashlib.sha256(b64.encode()).hexdigest()[:16]
                results[key] = b64
        except Exception:  # noqa: BLE001
            continue
    return results


def run(ctx, inputs, run_dir, session):
    script_path = Path(inputs["script"])
    script = json.loads(script_path.read_text(encoding="utf-8"))
    image_count = int(inputs.get("image_count", 6))
    prompts = script.get("image_prompts", []) or []
    if len(prompts) < image_count:
        prompts = (prompts * ((image_count // max(1, len(prompts))) + 1))[:image_count]
    prompts = prompts[:image_count]

    page = session.first_page()
    from ...resilience.interaction import ElementInteractor

    ux = ElementInteractor(page)
    ux.goto(GENERATOR_URL, wait_until="domcontentloaded")
    time.sleep(12)  # let the generator iframe + UI finish loading
    _generator_frame(page)

    assets_dir = run_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    saved: list = []

    for idx, prompt in enumerate(prompts):
        gen = _generator_frame(page)
        before = set(_collect_finished_images(page))

        box = gen.locator(PROMPT_SELECTOR).first
        box.click(timeout=15000)
        box.fill("", timeout=8000)
        box.type(prompt, delay=6)

        generate = gen.locator(GENERATE_SELECTOR).first
        ux.click(generate, description="perchance generate")

        # wait for new images to appear for this generation
        got = None
        t0 = time.time()
        while time.time() - t0 < MAX_PER_IMAGE_SEC:
            time.sleep(POLL_INTERVAL_SEC)
            cur = _collect_finished_images(page)
            new_keys = [k for k in cur if k not in before]
            if new_keys:
                got = {k: cur[k] for k in new_keys}
                break
            before = set(cur)

        if not got:
            log.warning("No finished image captured for prompt %d; skipping", idx)
            continue

        # save the first finished image from this batch
        key = list(got.keys())[0]
        b64 = got[key]
        try:
            data = base64.b64decode(b64)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not decode image for prompt %d: %s", idx, exc)
            continue
        fname = assets_dir / f"bg_{idx:02d}.jpg"
        fname.write_bytes(data)
        saved.append(str(fname))
        log.info("Saved asset %d -> %s (%d bytes)", idx, fname.name, len(data))
        time.sleep(3)

    if not saved:
        raise RuntimeError("Perchance produced no saved images")

    return {"images": str(assets_dir), "image_files": saved}
