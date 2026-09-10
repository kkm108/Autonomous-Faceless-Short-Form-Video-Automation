"""Generate the voiceover for a Short.

Strategy is ordered and resilient by design. We first try the in-browser web TTS
tool (SoundTools / Kokoro) -- the original no-API, no-account path that fits the
project's "browser-automate external steps" ethos. The browser is flaky: the
Kokoro WASM model is fetched from a CDN that can fail to load, and the UI changes.
So we bound that attempt and, on any failure, fall back to high-quality local
TTS that needs no browser, ending with an offline engine that is guaranteed to
produce audio:

    1. soundtools  (browser, bounded + quick-fail)  -> WAV
    2. edge_tts    (Microsoft Edge neural voices)    -> MP3
    3. pyttsx3      (offline, always available)      -> WAV

Set config.TTS_PROVIDER to one of soundtools|edge_tts|pyttsx3 to force a single
path; "auto" (default) runs the full chain.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from ... import config

log = logging.getLogger(__name__)

TTS_URL = "https://soundtools.io/text-to-speech/"

# Semantic locator groups (fallback-ordered) for the browser tool.
LOCS = {
    "input": [
        "textarea",
        "div[contenteditable='true']",
        "input[type='text']",
    ],
    "generate": [
        "button:has-text('Generate Speech')",
        "button:has-text('Generate')",
        "button:has-text('Create Speech')",
        "button:has-text('Convert')",
        "button:has-text('Speak')",
        "[type='submit']",
    ],
    "download_wav": [
        "a:has-text('Download WAV')",
        "button:has-text('Download WAV')",
        "a:has-text('Download')",
        "[class*='download']",
        "a[download]",
    ],
    "model_error": [
        "text=Failed to load model",
        "div:has-text('Failed to load model')",
    ],
}


def _browser_soundtools(page, text: str, run_dir: Path, timeout_s: int,
                        settings=None) -> Path:
    """Attempt SoundTools in the given browser. Raises on any failure."""
    from ...resilience.interaction import ElementInteractor
    from ...resilience.location import ProviderLocations

    ux = ElementInteractor(page, provider="tts", settings=settings)
    locs = ProviderLocations(LOCS)

    ux.goto(TTS_URL, wait_until="domcontentloaded")

    # Fail fast if the page tells us the model CDN is broken.
    try:
        if locs.try_resolve(page, "model_error", timeout=15000) is not None:
            raise RuntimeError("SoundTools model failed to load (CDN error)")
    except Exception:
        raise

    input_box = locs.resolve(page, "input")
    input_box.click(timeout=15000)
    ux.fill(input_box, text, description="tts paste script")

    generate = locs.try_resolve(page, "generate")
    if generate is None:
        raise RuntimeError("SoundTools generate control not found")
    generate.click(timeout=10000)
    log.info("TTS (soundtools) generation started; waiting for download...")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        # bail if the model errored after generation started
        if locs.try_resolve(page, "model_error", timeout=1500) is not None:
            raise RuntimeError("SoundTools model failed to load (CDN error)")
        dl_loc = locs.try_resolve(page, "download_wav", timeout=2500)
        if dl_loc is not None:
            try:
                with page.expect_download(timeout=60000) as dl_info:
                    dl_loc.click(timeout=10000)
                download = dl_info.value
                wav_path = run_dir / (download.suggested_filename or "voiceover.wav")
                download.save_as(str(wav_path))
                log.info("Voiceover downloaded (soundtools): %s", wav_path.name)
                return wav_path
            except Exception as exc:  # noqa: BLE001
                log.warning("SoundTools download attempt failed (%s); retrying", exc)
                time.sleep(5)
        else:
            time.sleep(8)
    raise RuntimeError("SoundTools did not produce a downloadable audio file in time")


def _edge_tts(text: str, run_dir: Path, voice: str) -> Path:
    """High-quality neural TTS via Microsoft Edge voices (needs internet).

    Uses edge-tts' streaming API so we get BOTH the audio file and per-word
    start/end timings (R8-B2). The default ``.save()`` discards WordBoundary
    data; streaming lets the assembly stage sync captions to reality instead of
    dividing the timeline equally. Run the asyncio loop in a worker thread so we
    never clash with a live Playwright sync event loop on the main thread.
    """
    import threading

    mp3 = run_dir / "voiceover_edge.mp3"
    words_path = run_dir / "word_timings.json"
    err: list[BaseException] = []

    def _worker():
        try:
            asyncio.run(_edge_stream_save(text, str(mp3), voice, str(words_path)))
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)

    worker = threading.Thread(target=_worker)
    worker.start()
    worker.join()
    if err:
        raise err[0]
    if not mp3.exists() or mp3.stat().st_size == 0:
        raise RuntimeError("edge-tts produced no audio")
    log.info("Voiceover generated via edge-tts (%s): %s", voice, mp3.name)
    return mp3


async def _edge_stream_save(text: str, out: str, voice: str,
                            words_out: str) -> None:
    """Stream edge-tts into ``out``, recording WordBoundary timings.

    WordBoundary offsets arrive in 100-nanosecond units (Speech protocol); we
    normalise to seconds so the assembly/timing consumers stay unit-free.
    """
    import edge_tts

    audio = bytearray()
    words: list = []
    comm = edge_tts.Communicate(text, voice)
    async for chunk in comm.stream():
        kind = chunk.get("type")
        if kind == "audio":
            audio.extend(chunk.get("data") or b"")
        elif kind == "WordBoundary":
            offset = int(chunk.get("offset") or 0)
            duration = int(chunk.get("duration") or 0)
            words.append({
                "word": chunk.get("text") or "",
                "start_s": round(offset / 10_000_000, 4),
                "end_s": round((offset + duration) / 10_000_000, 4),
            })
    Path(out).write_bytes(bytes(audio))
    Path(words_out).write_text(json.dumps({
        "provider": "edge_tts",
        "voice": voice,
        "words": words,
        "total_s": round(words[-1]["end_s"], 4) if words else 0.0,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _pyttsx3(text: str, run_dir: Path) -> Path:
    """Offline TTS via the local OS speech engine (guaranteed to produce audio)."""
    import pyttsx3
    wav = run_dir / "voiceover_pyttsx3.wav"
    engine = pyttsx3.init()
    engine.save_to_file(text, str(wav))
    engine.runAndWait()
    engine.stop()
    if not wav.exists() or wav.stat().st_size == 0:
        raise RuntimeError("pyttsx3 produced no audio")
    log.info("Voiceover generated via pyttsx3: %s", wav.name)
    return wav


def run(ctx, inputs, run_dir, session=None):
    script = json.loads(Path(inputs["script"]).read_text(encoding="utf-8"))
    text = script.get("spoken_script", "").strip()
    if not text:
        raise ValueError("No spoken_script in script for TTS stage")

    provider = (ctx.settings.tts_provider or "auto").strip().lower()
    from ...channels import resolve_language
    from .routing import build_tts_chain, detect_language, edge_voice_for

    # R8-A2: the channel registry's language is the single fact driving the
    # provider chain and the Edge voice; text detection only remains as the
    # no-channel fallback.
    language = (resolve_language(ctx.settings.channel_name, text) if ctx else
                (detect_language(text) or "en"))
    chain = build_tts_chain(text, provider, language=language)
    voice = edge_voice_for(language)

    page = None
    if session is not None:
        try:
            page = session.first_page()
        except Exception:  # noqa: BLE001
            page = None

    used_method = None
    for method in chain:
        try:
            if method == "soundtools":
                if page is None:
                    raise RuntimeError("no browser session for soundtools")
                path = _browser_soundtools(page, text, run_dir,
                                       config.TTS_BROWSER_TIMEOUT_S,
                                       settings=ctx.settings)
            elif method == "edge_tts":
                path = _edge_tts(text, run_dir, voice)
            elif method == "pyttsx3":
                path = _pyttsx3(text, run_dir)
            elif method == "vagdhenu":
                if not config.VAGDHENU_ENABLED:
                    raise RuntimeError(
                        "Vagdhenu route disabled (AUTOMATO_VAGDHENU_ENABLED=1)")
                raise RuntimeError(
                    "Vagdhenu adapter is not yet wired; no Sanskrit content "
                    "in the pipeline")
            else:
                raise ValueError(f"Unknown TTS provider: {method}")
            used_method = method
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("TTS provider '%s' failed: %s", method, exc)
            # last-chance 'auto' is pyttsx3 which is offline; if even that fails,
            # propagate the error (nothing else to try).

    if used_method is None:
        raise RuntimeError("All TTS providers failed")

    # Always ship a word-timings sidecar (R8-B2): real WordBoundary data when
    # edge-tts served the run, otherwise an honest "no real timing" marker the
    # assembly stage turns into its documented per-slide estimation. This keeps
    # the manifest binding stable across every provider.
    timings_path = run_dir / "word_timings.json"
    if not timings_path.exists():
        timings_path.write_text(json.dumps({
            "provider": used_method,
            "voice": voice if used_method == "edge_tts" else None,
            "words": [],
            "estimated": True,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"audio": str(path), "word_timings": str(timings_path)}
