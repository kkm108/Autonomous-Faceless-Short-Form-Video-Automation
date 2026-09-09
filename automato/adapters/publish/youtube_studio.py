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


_VIDEO_LINK_PATTERNS = (
    r"youtu\.be/([A-Za-z0-9_-]{11})",
    r"[?&]v=([A-Za-z0-9_-]{11})",
    r"/shorts/([A-Za-z0-9_-]{11})",
    r"/video/([A-Za-z0-9_-]{11})(?:/|$|\?)",
)

# Studio truncates long titles in row text; match on a safe prefix so a truncated
# row still contains the needle (a full-title needle silently missed after Publish).
_TITLE_MATCH_PREFIX = 28


def _id_from_href(href: str) -> Optional[str]:
    """Extract an 11-char YouTube video id from a structurally-shaped href
    (youtu.be, watch?v=, /shorts/, /video/<id>/edit). Bare 11-char regexes over
    arbitrary strings are avoided on purpose (too many false id hits)."""
    href = href or ""
    for pattern in _VIDEO_LINK_PATTERNS:
        m = re.search(pattern, href)
        if m:
            return m.group(1)
    return None


def _row_text(page, link) -> str:
    try:
        return (link.evaluate(
            "e => {"
            "  const r = e.closest('ytcp-video-row')"
            "    || e.closest('ytcp-video-upload-status')"
            "    || e.closest('tp-yt-paper-list-item')"
            "    || e.parentElement;"
            "  return r ? (r.textContent || '') : ''; }"
        ) or "")
    except Exception:  # noqa: BLE001
        return ""


def _candidate_links(page, scope: str):
    sels = (
        f"{scope} a[href*='youtu.be']",
        f"{scope} a[href*='/watch?v=']",
        f"{scope} a[href*='/shorts/']",
        f"{scope} a[href*='/video/']",
    )
    for sel in sels:
        try:
            links = page.locator(sel)
            for i in range(min(links.count(), 60)):
                try:
                    yield links.nth(i)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            continue


def _extract_id_for_title(page, title: str, scope: str = "body") -> Optional[str]:
    """Scoped URL extraction: only accept a video link whose enclosing row names
    the freshly uploaded title. A naive page-wide first match latched onto a
    stale video id for several runs (R6 surveillance finding); title-scoping makes
    the capture unambiguous."""
    needle = (title or "").strip()[:_TITLE_MATCH_PREFIX]
    if not needle:
        return None
    for link in _candidate_links(page, scope):
        try:
            href = link.get_attribute("href", timeout=3000) or ""
        except Exception:  # noqa: BLE001
            continue
        if not href or not any(k in href for k in
                               ("youtu.be", "/watch?v=", "/shorts/", "/video/")):
            continue
        try:
            link_text = link.inner_text(timeout=2000) or ""
        except Exception:  # noqa: BLE001
            link_text = ""
        if needle not in link_text and needle not in _row_text(page, link):
            continue
        vid = _id_from_href(href)
        if vid:
            return vid
    return None


def _known_video_ids(exclude_run_dir: Path, root: Optional[Path] = None) -> set:
    """Every video id previously recorded under output/. Guard against re-capturing
    a stale id that an earlier publish already claimed."""
    ids = set()
    if root is None:
        root = Path(__file__).resolve().parents[3] / "output"
    try:
        for post in root.glob("*/post_url.json"):
            try:
                if exclude_run_dir is not None and post.parent == exclude_run_dir:
                    continue
                url = (json.loads(post.read_text(encoding="utf-8")) or {}).get("url", "")
                vid = _id_from_href(url)
                if vid:
                    ids.add(vid)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return ids


def _goto_videos_list(page, shorts: bool = True) -> None:
    """Navigate to Studio's Content list the way it actually works, optionally
    onto the Shorts tab.

    Deep-linking straight to ``studio.youtube.com/videos`` renders
    "Oops, something went wrong." (observed repeatedly in probes), while going
    to the Studio root and clicking the "Content" nav item reliably renders the
    row list. Uploaded shorts additionally live under the "Shorts" tab — the
    "Videos" tab (the active default) won't list them (R6 probe observation), so
    we switch there when looking for a just-published short."""
    try:
        page.goto("https://studio.youtube.com/", wait_until="domcontentloaded",
                  timeout=30000)
        time.sleep(config.YOUTUBE_UI_SETTLE_S)
    except Exception:  # noqa: BLE001
        pass
    for getter in (
        lambda: page.get_by_role("link", name="Content"),
        lambda: page.get_by_text("Content", exact=True).first,
    ):
        try:
            loc = getter()
            if loc.count() > 0:
                loc.click(timeout=6000)
                break
        except Exception:  # noqa: BLE001
            continue
    if shorts:
        for getter in (
            lambda: page.get_by_role("tab", name="Shorts"),
            lambda: page.locator("tp-yt-paper-tab:has-text('Shorts')", has_text="Shorts"),
            lambda: page.get_by_text("Shorts", exact=True).first,
        ):
            try:
                loc = getter()
                if loc.count() > 0:
                    loc.click(timeout=6000)
                    break
            except Exception:  # noqa: BLE001
                continue
    time.sleep(config.YOUTUBE_UI_SETTLE_S)


def _id_from_video_list(page, title: str, timeout_s: int = 420) -> Optional[str]:
    """Patiently watch Studio's Videos list for the row named ``title`` and
    return its video id.

    A just-published short is still *processing* right after Publish: it is not
    yet present in the Content list (and the upload dialog has closed), so a
    quick scan returns nothing. The row reliably appears within a couple of
    minutes, so we scan until it does, re-visiting the list each pass to avoid a
    stale state, bounded by ``timeout_s``."""
    needle = (title or "").strip()[:_TITLE_MATCH_PREFIX]
    if not needle:
        return None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _goto_videos_list(page)
        scan_until = time.time() + 20
        while time.time() < scan_until:
            try:
                rows = page.locator("ytcp-video-row")
                for i in range(min(rows.count(), 80)):
                    try:
                        row = rows.nth(i)
                        if needle and needle not in (row.inner_text(timeout=3000) or ""):
                            continue
                        href = row.locator("a[href*='/video/']").first.get_attribute(
                            "href", timeout=3000) or ""
                        vid = _id_from_href(href)
                        if vid:
                            log.info("Found uploaded video row in Studio list: %s -> %s",
                                     (row.inner_text(timeout=2000) or "").strip()[:_TITLE_MATCH_PREFIX],
                                     vid)
                            return vid
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)
        time.sleep(5)
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
        _goto_videos_list(page)
        # The video-list rows render as ytcp-video-row elements or title cells.
        deadline = time.time() + timeout_s
        needle = (title or "").strip()[:_TITLE_MATCH_PREFIX]
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
        # Reuse the ALREADY-OPEN provider session (same process retry/resume):
        # opening a fresh sync session here crashes with "Playwright Sync API
        # inside the asyncio loop" when the orchestrator retries in-process.
        try:
            page_check = session.first_page()
            session_mod.run_auth_check("youtube", session)
            title_hint = (json.loads(
                (run_dir / "upload_attempted.json").read_text(encoding="utf-8")
            ).get("title") or "")
            if _recent_upload_exists(page_check, title_hint):
                # The upload did go through; record it and treat as done.
                vid = _extract_id_for_title(page_check, title_hint) or ""
                url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
                out_path = run_dir / "post_url.json"
                if url:
                    out_path.write_text(json.dumps(
                        {"status": "uploaded", "visibility": "unlisted",
                         "url": url}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    return {"post_url": str(out_path), "url": url,
                            "visibility": "unlisted"}
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

    # 5. Title. YouTube pre-fills the title from the source filename
    #    (final.mp4 -> "final"); clear the box first so we don't end up with a
    #    "final<Script Title>" prefix on every upload (R6 surveillance finding).
    title_box = locs.resolve(page, "title_box")
    ux.click(title_box, description="title box", loc_group="title_box")
    try:
        title_box.fill("", timeout=8000)
    except Exception:  # noqa: BLE001
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
        except Exception:  # noqa: BLE001
            pass
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

    # 11. Extract URL — title-scoped only. A page-wide first-match scan latched
    #     onto a stale video id across many runs; we now require the link's row
    #     to name this run's title, then patiently watch the Studio list for the
    #     still-processing upload (which is why an immediate scan finds nothing).
    recognized = _known_video_ids(run_dir)
    vid = (
        _extract_id_for_title(page, title, scope="ytcp-uploads-dialog")
        or _extract_id_for_title(page, title, scope="body")
    )
    if vid and vid in recognized:
        log.warning("Dialog/body link named id '%s' already recorded by an "
                    "earlier run; treating as stale", vid)
        vid = None
    if vid is None:
        log.info("New upload not yet capturable from current page; watching the "
                 "Studio Videos list for '%s'", title)
        vid = _id_from_video_list(page, title,
                                  timeout_s=config.YOUTUBE_PUBLISH_LISTING_WAIT_S)
    if vid and vid in recognized:
        log.warning("Studio list link named id '%s' already recorded by an "
                    "earlier run; refusing to record a duplicate", vid)
        vid = None
    url = f"https://www.youtube.com/watch?v={vid}" if vid else None
    if url is None:
        log.warning("Could not extract a non-colliding video URL for title '%s'",
                    title)

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
