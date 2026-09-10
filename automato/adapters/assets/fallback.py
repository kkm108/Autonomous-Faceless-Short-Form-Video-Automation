"""Keyless fallback image generation for the assets stage (R8-B1).

When the browser-based Perchance generator fails to produce an image (browser
flakiness, CDN issues, UI changes), the assets stage tops up from this module so
the pipeline survives. No API key, no signup, no account: a plain HTTP GET.

Provider order (vetting pass, both keyless):
  1. pollinations.ai image API  -- free text-to-image; the anonymous tier's
     images carry a small watermark, so runs that use it MUST be flagged as
     degraded (the asset quality gate soft-fails and publish downgrades to
     unlisted). Best prompt fidelity.
  2. picsum.photos               -- infinite curated CC photos, no watermark,
     prompt-independent. Last resort so a dead image API never kills a run.

Outputs are normalised to exact 1080x1920 JPEGs regardless of what the provider
returned, so downstream assembly sees the same artifact shape as Perchance.
"""
from __future__ import annotations

import io
import logging
import random
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

USER_AGENT = "automato-runtime/1.0 (local engine; contact: self-hosted)"
W, H = 1080, 1920
PROMPT_LIMIT = 300  # chars; over-long AI prompts just add latency/noise


def _fetch(url: str, timeout_s: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
    if not data:
        raise RuntimeError(f"empty response from {url}")
    return data


def generate_image(prompt: str, out_path: Path, width: int = W, height: int = H,
                   timeout_s: int = 90) -> tuple:
    """Fetch one fallback image for ``prompt`` and save it as a JPEG.

    Returns ``(out_path, source)`` where source is 'pollinations' or 'picsum'.
    Raises RuntimeError when every provider fails.
    """
    seed = random.randrange(1 << 30)
    capped = (prompt or "").strip()[:PROMPT_LIMIT].replace(" ", "+")
    data = None
    source = None
    try:
        url = (f"https://image.pollinations.ai/prompt/"
               f"{urllib.parse.quote(capped)}"
               f"?width={width}&height={height}&seed={seed}")
        data = _fetch(url, timeout_s)
        source = "pollinations"
        log.info("Fallback image generated (pollinations): %s", out_path.name)
    except Exception as exc:  # noqa: BLE001
        log.warning("pollinations fallback failed (%s); trying picsum", exc)
    if not data:
        try:
            url = f"https://picsum.photos/seed/{seed & 0xFFFFFFFF}/{width}/{height}"
            data = _fetch(url, timeout_s)
            source = "picsum"
            log.info("Fallback image fetched (picsum): %s", out_path.name)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Both fallback image providers failed for prompt {prompt!r}: {exc}"
            ) from exc

    im = Image.open(io.BytesIO(data)).convert("RGB").resize((width, height),
                                                            Image.LANCZOS)
    im.save(out_path, "JPEG", quality=90)
    return out_path, source
