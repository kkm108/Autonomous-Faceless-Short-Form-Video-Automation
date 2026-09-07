"""Publish the finished video to YouTube Shorts via YouTube Studio web UI.

Entirely browser-driven through the persistent Edge profile. The flow mirrors a
real upload and uses semantic/ARIA locators:
  1. Studio -> Create -> Upload videos
  2. set_input_files on the hidden file input
  3. wait for the upload dialog, fill title/description, set "Not for kids"
  4. Next -> Next -> Next (Details -> Elements -> Checks)
  5. choose visibility (unlisted by default)
  6. Publish; extract the resulting video URL from the success dialog

No YouTube Data API / OAuth — just the web app, piggybacking on the stored login.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from ... import config
from ...browser import session as session_mod
from ..base import ExecutorError

log = logging.getLogger(__name__)

STUDIO_URL = "https://studio.youtube.com/"

LOCS = {
    "create": [
        "button[aria-label='Create']",
        "ytcp-button#create-icon",
        "button:has-text('Create')",
    ],
    "upload_item": [
        "ytcp-ve#label[aria-label*='Upload']",
        "yt-formatted-string:has-text('Upload videos')",
        "tp-yt-paper-item:has-text('Upload videos')",
    ],
    "file_input": ["input[type='file']"],
    "upload_dialog": ["ytcp-uploads-dialog"],
    "title_box": [
        "#textbox[aria-label*='title' i]",
        ".ytcp-text-input",
        "#title-textarea #textbox",
        "div[contenteditable='true'][aria-label*='title' i]",
    ],
    "desc_box": [
        "#description-textarea #textbox",
        "div[contenteditable='true'][aria-label*='description' i]",
    ],
    "not_for_kids": [
        "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
        "tp-yt-paper-radio-button:has-text('No, it'\\''s not made for kids')",
        "ytcp-ve:has-text('No, it'\\''s not made for kids')",
    ],
    "next": ["#next-button", "ytcp-button#next-button"],
    "done_publish": [
        "button[aria-label*='Publish']",
        "ytcp-button:has-text('Publish')",
        "ytcp-button:has-text('Done')",
        "#done-button",
    ],
    "video_link": [
        "a[href*='youtu.be']",
        "a[href*='youtube.com/watch']",
        "a[href*='/video/']",
    ],
    "visibility": [
        "tp-yt-paper-radio-button[name='{v}']",
    ],
    "close_dialog": [
        "ytcp-button:has-text('Close')",
        "ytcp-button:has-text('Done')",
        "button[aria-label='Close']",
        "ytcp-button#close-button",
    ],
}


def _extract_url(page):
    for sel in LOCS["video_link"]:
        try:
            link = page.locator(sel).first
            href = link.get_attribute("href", timeout=4000)
            if href:
                return href if href.startswith("http") else f"https://youtube.com{href}"
        except Exception:  # noqa: BLE001
            continue
    return None


def _extract_url_after_recent(page):
    """On the Studio Videos list, find a video row's link/URL if available."""
    try:
        links = page.locator("a[href*='youtu.be'], a[href*='/watch?v=']")
        for i in range(links.count()):
            href = links.nth(i).get_attribute("href", timeout=3000)
            if href:
                return href if href.startswith("http") else f"https://youtube.com{href}"
    except Exception:  # noqa: BLE001
        pass
    return None


def _load_previous(run_dir: Path):
    """Return a prior publish result dict if this run already published."""
    out_path = run_dir / "post_url.json"
    if not out_path.exists():
        return None
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return payload if payload.get("url") else None


def _upload_attempted(run_dir: Path) -> Optional[float]:
    """Timestamp (epoch) of the last upload attempt, if any.

    Written *before* clicking Publish so a resume can detect that an upload may
    have gone through even though ``post_url.json`` was never written (R1-W3).
    """
    path = run_dir / "upload_attempted.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return float(payload.get("attempted_at", 0))
    except Exception:  # noqa: BLE001
        return None


def _mark_upload_attempted(run_dir: Path, title: str) -> None:
    """Persist an upload-attempt marker before the Publish click."""
    path = run_dir / "upload_attempted.json"
    path.write_text(json.dumps({
        "attempted_at": time.time(),
        "title": title,
        "visibility": "unlisted",
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _recent_upload_exists(page, title: str, timeout_s: int = 60) -> bool:
    """Navigate YouTube Studio's Videos list and check for ``title`` among recent
    uploads. Used on resume to avoid double-publishing when the previous attempt
    succeeded but never wrote ``post_url.json``.
    """
    try:
        page.goto("https://studio.youtube.com/videos", wait_until="domcontentloaded",
                  timeout=30000)
        # The video-list rows render as ytcp-video-row elements or title cells.
        deadline = time.time() + timeout_s
        needle = (title or "").strip()[:80]
        while time.time() < deadline:
            try:
                content = page.locator("body").inner_text(timeout=4000)
                if needle and needle in content:
                    return True
                # "No videos" empty-state: definitely not present.
                empty = page.locator("text=You haven't uploaded any videos yet")
                if empty.count() > 0:
                    return False
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)
        return False
    except Exception:  # noqa: BLE001
        return False


def run(ctx, inputs, run_dir, session):
    video_path = Path(inputs["video"])
    script = json.loads(Path(inputs["script"]).read_text(encoding="utf-8"))
    if not video_path.exists():
        raise ExecutorError(f"Video not found: {video_path}", retryable=False)

    # Idempotency: if this run already published successfully, do not re-upload.
    prev = _load_previous(run_dir)
    if prev is not None:
        log.info("Run already published (%s); skipping re-upload", prev.get("url"))
        return {
            "post_url": str(run_dir / "post_url.json"),
            "url": prev["url"],
            "visibility": prev.get("visibility", ctx.settings.visibility),
        }

    # R1-W3 idempotency gap: the previous attempt may have published but never
    # wrote post_url.json. On resume, check Studio's own recent-uploads list
    # before re-submitting the file input to avoid publishing the video twice.
    attempted = _upload_attempted(run_dir)
    if attempted is not None:
        page_check = None
        try:
            bs, _ = session_mod.open_session("youtube", settings=ctx.settings)
            try:
                page_check = bs.first_page()
                session_mod.run_auth_check("youtube", bs)
                title_hint = (json.loads(
                    (run_dir / "upload_attempted.json").read_text(encoding="utf-8")
                ).get("title") or "")
                if _recent_upload_exists(page_check, title_hint):
                    # The upload did go through; record it and treat as done.
                    url = _extract_url_after_recent(page_check) or ""
                    out_path = run_dir / "post_url.json"
                    if url:
                        out_path.write_text(json.dumps(
                            {"status": "uploaded", "visibility": "unlisted",
                             "url": url}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
                        return {"post_url": str(out_path), "url": url,
                                "visibility": "unlisted"}
            finally:
                try:
                    bs.close()
                finally:
                    session_mod.release_session_lock("youtube")
        except Exception as exc:  # noqa: BLE001
            raise ExecutorError(
                f"Previous upload may have succeeded but cannot be confirmed; "
                f"check YouTube Studio manually. ({exc})",
                retryable=True,
            )
        raise ExecutorError(
            "Previous upload may have succeeded (upload was attempted but no URL "
            "was recorded) and it was not found in recent uploads. Check YouTube "
            "Studio manually before resuming; refusing to re-upload blind.",
            retryable=False,
        )

    visibility = ctx.settings.visibility
    if ctx.settings.force_unlisted and visibility != "unlisted":
        # A soft-fail quality gate tripped somewhere in this run (R2-W4/R2-F4):
        # last safety net — never let a degraded run go live publicly.
        log.warning("A quality gate soft-failed this run; downgrading visibility "
                    "from '%s' to 'unlisted'", visibility)
        visibility = "unlisted"
    title = (script.get("title") or "Untitled")[:100]
    description = "Automated faceless short.\n\n#shorts"

    page = session.first_page()
    from ...resilience.interaction import ElementInteractor
    from ...resilience.location import ProviderLocations

    ux = ElementInteractor(page, provider="youtube", settings=ctx.settings)
    locs = ProviderLocations(LOCS, provider="youtube")

    ux.goto(STUDIO_URL, wait_until="domcontentloaded")
    time.sleep(config.YOUTUBE_UI_SETTLE_S)

    # 1. Create button
    create = locs.resolve(page, "create")
    ux.click(create, description="studio create", loc_group="create")

    # 2. Upload videos menu item
    upload_item = locs.resolve(page, "upload_item")
    ux.click(upload_item, description="upload videos menu", loc_group="upload_item")
    time.sleep(config.YOUTUBE_POST_CLICK_SLEEP_S)

    # 3. Set file (the input is hidden in the DOM; must not be scrolled into view)
    file_input = locs.resolve_hidden(page, "file_input")
    ux.upload(file_input, str(video_path), description="select video file", loc_group="file_input")

    # 4. Wait for the upload dialog to finish processing and reach the DETAILS
    #    form (the dialog element itself is hidden; watch its workflow-step attr).
    dialog = locs.resolve_hidden(page, "upload_dialog")
    deadline = time.time() + config.YOUTUBE_UPLOAD_DEADLINE_S
    while time.time() < deadline:
        try:
            step = (dialog.get_attribute("workflow-step", timeout=2000) or "").upper()
        except Exception:  # noqa: BLE001
            step = ""
        if step == "DETAILS":
            break
        time.sleep(config.YOUTUBE_POST_CLICK_SLEEP_S)
    time.sleep(config.YOUTUBE_UI_SETTLE_S)

    # 5. Title
    title_box = locs.resolve(page, "title_box")
    ux.click(title_box, description="title box", loc_group="title_box")
    ux.type_text(title_box, title, delay_ms=20, description="title", loc_group="title_box")

    # 6. Description
    desc_box = locs.try_resolve(page, "desc_box", timeout=3000)
    if desc_box is not None:
        ux.type_text(desc_box, description, delay_ms=10, description="description",
                     loc_group="desc_box")

    # 7. Not for kids (force the click: a hashtag-suggestion dropdown sometimes
    #    floats over the radio and intercepts pointer events).
    kids = locs.try_resolve(page, "not_for_kids", timeout=3000)
    if kids is not None:
        ux.click(kids, description="not for kids", loc_group="not_for_kids", force=True)

    # 8. Next x3
    for step in range(3):
        next_btn = locs.try_resolve(page, "next", timeout=5000)
        if next_btn is None:
            break
        ux.click(next_btn, description=f"next step {step + 1}", loc_group="next")
        time.sleep(config.YOUTUBE_POST_CLICK_SLEEP_S)

    # 9. Visibility
    vis_btn = page.locator(f"tp-yt-paper-radio-button[name='{visibility.upper()}']").first
    try:
        ux.click(vis_btn, description=f"set visibility {visibility}", loc_group="visibility")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not select visibility (%s); using default radio", exc)

    # 10. Publish / Done. Write a durable "upload attempted at T" marker BEFORE
    #     clicking so a resume can detect a possibly-succeeded upload even if
    #     post_url.json is never written (R1-W3).
    _mark_upload_attempted(run_dir, title)
    done = locs.resolve(page, "done_publish")
    ux.click(done, description="publish/done", loc_group="done_publish")
    time.sleep(config.YOUTUBE_POST_PUBLISH_SLEEP_S)

    # 11. Extract URL
    url = _extract_url(page)
    if url is None:
        log.warning("Could not extract video URL directly; scanning page text")
        try:
            body = page.locator("body").inner_text(timeout=4000)
            m = re.search(r"(https?://(?:www\.)?youtube\.com/(?:watch\?v=|shorts/)[^\s\"']+)", body)
            if m:
                url = m.group(1)
        except Exception:  # noqa: BLE001
            pass

    # close dialog
    try:
        close_btn = locs.try_resolve(page, "close_dialog", timeout=3000)
        if close_btn is not None:
            ux.click(close_btn, description="close dialog", loc_group="close_dialog")
    except Exception:  # noqa: BLE001
        pass

    if not url:
        # Never report a successful publish without a URL: that would let the
        # orchestrator mark the run done while the outcome is unknown, and would
        # hide a possible duplicate on resume. Fail retryably instead.
        raise ExecutorError(
            "Upload ran but no video URL could be extracted; the upload may still "
            "have succeeded. Check YouTube Studio manually before resuming.",
            retryable=True,
        )

    result = {"status": "uploaded", "visibility": visibility, "url": url}
    out_path = run_dir / "post_url.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Publish result: %s", result)
    return {"post_url": str(out_path), "url": url, "visibility": visibility}
