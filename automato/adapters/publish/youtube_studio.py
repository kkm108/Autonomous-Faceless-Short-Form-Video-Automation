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
import sys
import time
from pathlib import Path
from typing import List, Optional

from ... import config
from ...browser import session as session_mod
from ...channels import active_channel_id, channel_id_for, ensure_channel
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
    # YouTube gates publishes while checks are pending with a
    # "Publish anyway" interstitial (observed on a claim-flagged channel).
    "publish_anyway": [
        "ytcp-button:has-text('Publish anyway')",
        "ytcp-button:has-text('Publish Anyway')",
        "button[aria-label*='publish anyway' i]",
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
            "    || e.closest('ytcp-uploads-dialog')"
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


def _omnisearch_results(page, query: str) -> List[tuple]:
    """Run Studio's own cross-channel search (header omnisearch) and return
    ``(row_text, video_id)`` pairs.

    The Content list scan is fragile: rows whose title text lives inside shadow
    DOM (or that sit beyond the first virtualized page) are invisible to
    ``inner_text`` even though they are uploaded. Studio's "Search across your
    channel" panel renders the same rows in a way we can read, so it is the
    authoritative fallback for confirming an upload and recovering its id."""
    ids = []
    try:
        for getter in (lambda: page.get_by_role("button", name="Search"),
                       lambda: page.locator("[aria-label='Search']")):
            loc = getter()
            if loc.count() > 0:
                loc.first.click(timeout=5000, force=True)
                break
    except Exception:  # noqa: BLE001
        pass
    q = page.locator("#query-input")
    if q.count() == 0:
        return []
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if q.first.is_visible(timeout=1000):
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    try:
        q.first.fill(query, timeout=5000, force=True)
        time.sleep(2)
        page.keyboard.press("Enter")
        time.sleep(5)
        ids = page.evaluate("""() => {
            const out = [];
            const root = document.querySelector('ytcp-omnisearch-results')
                || document.querySelector('ytcp-omnisearch') || document;
            const seen = new Set();
            for (const a of root.querySelectorAll(
                    'a[href*="/video/"], a[href*="/shorts/"], a[href*="/watch"], a[href*="youtu.be"]')) {
                const row = a.closest('ytcp-video-search-row, ytcp-video-row, li')
                    || a.parentElement;
                out.push({ text: (row.textContent || '').replace(/\\s+/g, ' ').slice(0, 300),
                           href: a.href || '' });
            }
            return out;
        }""")
    except Exception:  # noqa: BLE001
        ids = []
    pairs = []
    for item in ids or []:
        vid = _id_from_href(item.get("href") or "")
        if vid and vid not in pairs and (item.get("text") or "") not in pairs:
            pairs.append((item.get("text") or "", vid))
    return pairs


def _omnisearch_ids_for_title(page, title: str, timeout_s: int = 25) -> Optional[str]:
    """Find a freshly-uploaded video id via Studio's cross-channel search."""
    needle = (title or "").strip()[:_TITLE_MATCH_PREFIX]
    if not needle:
        return None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            for text, vid in _omnisearch_results(page, title.strip()[:40]):
                if needle and needle in text:
                    log.info("Found uploaded video via cross-channel search: %s", vid)
                    return vid
        except Exception:  # noqa: BLE001
            pass
        time.sleep(4)
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


def _mark_upload_attempted(run_dir: Path, title: str, channel_name: str) -> None:
    """Persist an upload-attempt marker before the Publish click."""
    path = run_dir / "upload_attempted.json"
    path.write_text(json.dumps({
        "attempted_at": time.time(),
        "title": title,
        "visibility": "unlisted",
        "channel": channel_name,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _checked_visibility(page, visibility: str, ux) -> None:
    """Click the visibility radio and confirm the selection actually applied.

    A name-targeted radio click that silently fails (or decoys) leaves the upload
    on the dialog's default PRIVATE radio — which then never shows a row in the
    Content lists and looks like a failed publish (E2E observation).
    """
    target = visibility.upper()
    for _ in range(3):
        try:
            vis_btn = page.locator(
                f"tp-yt-paper-radio-button[name='{target}']").first
            ux.click(vis_btn, description=f"set visibility {visibility}",
                     loc_group="visibility")
        except Exception as exc:  # noqa: BLE001
            log.warning("visibility radio click failed (%s)", exc)
        time.sleep(config.YOUTUBE_POST_CLICK_SLEEP_S)
        current = _selected_visibility(page)
        if current is not None and target in current.upper():
            log.info("Visibility '%s' confirmed on the upload dialog", visibility)
            return
        try:
            label = page.locator(
                f"tp-yt-paper-radio-button:has-text('{visibility}')").first
            ux.click(label, description=f"set visibility {visibility} (label)",
                     loc_group="visibility")
        except Exception as exc:  # noqa: BLE001
            log.warning("visibility label click failed (%s)", exc)
    log.warning("Could not verify visibility '%s'; checked radio reads '%s'",
                visibility, _selected_visibility(page) or "?")


def _selected_visibility(page) -> Optional[str]:
    """Read the currently-checked radio's label text inside the upload dialog."""
    try:
        dialog = page.locator("ytcp-uploads-dialog").first
        for radio in dialog.locator(
                "tp-yt-paper-radio-button[aria-checked='true'], "
                "tp-yt-paper-radio-button.iron-selected, "
                "tp-yt-paper-radio-button[checked]").all():
            txt = (radio.inner_text(timeout=1500) or "").strip()
            if txt:
                return txt
    except Exception:  # noqa: BLE001
        pass
    return None


def _confirm_publish_anyway(page, ux) -> None:
    """If YouTube gates the publish behind its pending-checks warning, click the
    required 'Publish anyway' confirmation button once it appears."""
    from ...resilience.location import ProviderLocations
    locs = ProviderLocations(LOCS, provider="youtube")
    deadline = time.time() + 12
    while time.time() < deadline:
        try:
            btn = locs.try_resolve(page, "publish_anyway", timeout=1500)
            if btn is not None:
                ux.click(btn, description="confirm publish anyway",
                         loc_group="publish_anyway", force=True)
                log.info("Published past the pending-checks warning "
                         "('Publish anyway' clicked)")
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    log.debug("No 'Publish anyway' gate present after the publish click")


def _verify_target_channel(page, ctx, url_scope: Optional[str] = None) -> str:
    """R7 hard pre-upload gate: route Studio onto the run's target channel and
    then RE-VERIFY that the active channel matches. Any mismatch aborts before a
    single upload action — never warn-and-continue."""
    channel_name = ctx.settings.channel_name or config.CHANNEL_DEFAULT
    target_id = channel_id_for(channel_name)
    if target_id is None:
        raise ExecutorError(
            f"No channel id for target channel '{channel_name}'; check the "
            "validated allowlist (automato/config.py or "
            "AUTOMATO_CHANNEL_WHITELIST).",
            retryable=False,
        )
    log.info("Publish target channel: %s (%s)", channel_name, target_id)
    ensure_channel(page, target_id)
    active = active_channel_id(page)
    if active != target_id:
        raise ExecutorError(
            f"Active Studio channel {active} does not match intended target "
            f"{target_id} ({channel_name}); aborting before upload.",
            retryable=False,
        )
    return channel_name


def _confirm_publish(settings, title: str, channel_name: str,
                     input_fn=None) -> bool:
    """R7 confirm-before-publish: explicit operator 'y' immediately before the
    Publish click; anything else aborts. Without an interactive terminal the
    default is to abort (safe), unless confirmation was disabled/--yes'd. An
    explicitly passed ``input_fn`` (tests, drivers) skips the tty gate."""
    if not settings.publish_confirm:
        return True
    interactive = False
    if input_fn is not None:
        interactive = True
    else:
        input_fn = sys.stdin.readline
        try:
            interactive = sys.stdin.isatty()
        except Exception:  # noqa: BLE001
            interactive = False
    if not interactive:
        raise ExecutorError(
            "Publish confirmation is enabled but stdin is not interactive. "
            "Re-run from a terminal (or pass --yes to pre-confirm). Aborting "
            "before publishing anything.",
            retryable=False,
        )
    answer = (input_fn(
        f"About to publish '{title}' to channel '{channel_name}'. "
        "Publish now? [y/N] ") or "").strip().lower()
    if answer not in ("y", "yes"):
        raise ExecutorError(
            "Aborted by operator before publish.",
            retryable=False,
        )
    return True


def _recent_upload_exists(page, title: str, timeout_s: int = 60) -> bool:
    """Navigate YouTube Studio's Videos list and check for ``title`` among recent
    uploads. Used on resume to avoid double-publishing when the previous attempt
    succeeded but never wrote ``post_url.json``.

    The list's ``inner_text`` can miss rows whose titles render inside shadow DOM
    or beyond the first virtualized page, so the cross-channel search is the
    authoritative fallback (observed: a published short invisible to the list but
    found instantly by omnisearch).
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
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)
    except Exception:  # noqa: BLE001
        return False
    # Authoritative fallback: Studio's own cross-channel search.
    return _omnisearch_ids_for_title(page, title, timeout_s=20) is not None


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

    # R7 hard pre-upload gate: the active Studio channel MUST be the run's
    # intended target before any upload action. Routes onto the target and
    # re-verifies; any mismatch aborts.
    channel_name = _verify_target_channel(page, ctx)

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

    # 9. Visibility. The name-targeted radio is not always live on the final
    #    step (and a failed selection silently leaves the upload PRIVATE, which
    #    then shows no row in the Content lists). Click the named radio, then
    #    VERIFY the checked radio actually reads the requested visibility,
    #    falling back to a text-labeled click.
    _checked_visibility(page, visibility, ux)

    # 10. Publish / Done. Write a durable "upload attempted at T" marker BEFORE
    #     clicking so a resume can detect a possibly-succeeded upload even if
    #     post_url.json is never written (R1-W3). R7: ask for an explicit
    #     confirm-before-publish here (aborts on anything but 'y').
    _confirm_publish(ctx.settings, title, channel_name)
    _mark_upload_attempted(run_dir, title, channel_name)
    done = locs.resolve(page, "done_publish")
    ux.click(done, description="publish/done", loc_group="done_publish")

    # 10b. Pending-checks gate: when publishing first triggers YouTube's
    #     "you may get a strike" warning, a second explicit "Publish anyway"
    #     click is required before the upload completes. Without it the video is
    #     never actually published (it stays private in the still-open dialog),
    #     which is exactly the silent-failure signature seen in E2E.
    _confirm_publish_anyway(page, ux)
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
    if vid is None:
        # The Content list can stay blind to a freshly-published short (shadow
        # DOM / pagination), while Studio's own cross-channel search finds it
        # immediately. Try it before declaring the URL unrecoverable.
        log.info("List watcher found nothing; searching across the channel for "
                 "'%s'", title)
        vid = _omnisearch_ids_for_title(page, title, timeout_s=30)
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

    result = {"status": "uploaded", "visibility": visibility, "url": url,
              "channel": channel_name}
    out_path = run_dir / "post_url.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Publish result: %s", result)
    return {"post_url": str(out_path), "url": url, "visibility": visibility,
            "channel": channel_name}
