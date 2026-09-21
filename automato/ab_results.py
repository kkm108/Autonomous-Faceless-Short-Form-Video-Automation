"""Provider-performance correlation (R11-W3).

Joins the provider *choice* a run recorded (``provider_choices.json``, or the
legacy markers) with the post-publish *performance* of that run's video
(``performance.json`` written by an ``ab-results --fetch`` pass). Rolls both up
per stage/per provider so the A/B question — "does the provider the weight roll
picked actually perform" — can be answered with real numbers ~7 days after
publish.

No-data is an expected state, not an error: a run published minutes ago has no
analytics yet, and ``fetch_performance`` deliberately records that as
``no_data`` and drops nothing. The rollup never raises on missing perf files — a
run with no data just counts in the "awaiting data" column.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import time
from pathlib import Path

from . import config
from .adapters.metrics import studio_metrics

log = logging.getLogger(__name__)

STAGES = ("tts", "assets", "llm")
_VIDEO_ID_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
# A no_data record is retryable, but not forever: the fetch may be hitting a
# video id that will never carry our analytics (e.g. an id strayed into the
# run's post_url from another channel), so each run gets a bounded number of
# attempts before it is retired instead of re-scraped on every future pass.
_NO_DATA_RETRY_CAP = 3


def _load_json(path) -> "dict | None":
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def iter_run_dirs(root=None):
    """Yield every run directory (has a run_state.json), oldest first."""
    root = Path(root) if root else config.OUTPUT_DIR
    if not root.is_dir():
        return
    for p in sorted(root.iterdir()):
        if p.is_dir() and (p / "run_state.json").is_file():
            yield p


def video_url(run_dir) -> "str | None":
    data = _load_json(run_dir / "post_url.json") or {}
    return data.get("url") or None


def video_id(run_dir) -> "str | None":
    url = video_url(run_dir)
    if not url:
        return None
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def published_age_days(run_dir) -> "float | None":
    """Age (days) of the published video via the post_url.json mtime."""
    p = Path(run_dir) / "post_url.json"
    if not p.is_file():
        return None
    return (time.time() - p.stat().st_mtime) / 86400.0


def run_provider_choices(run_dir) -> dict:
    """The authoritative per-stage provider records (may be empty)."""
    data = _load_json(run_dir / "provider_choices.json") or {}
    return dict(data.get("stages") or {})


def legacy_choices(run_dir) -> dict:
    """Back-fill stage providers from pre-R11 artifacts (best-effort)."""
    out: dict = {}
    wt = _load_json(run_dir / "word_timings.json")
    if wt and wt.get("provider"):
        out["tts"] = {
            "provider": wt["provider"], "chosen_by": "default",
            "degraded": False, "legacy": True,
        }
    assets = _load_json(run_dir / "degraded_assets.json")
    out["assets"] = {
        "provider": "fallback" if assets else "perchance",
        "chosen_by": "default", "degraded": bool(assets), "legacy": True,
    }
    return out


def performance(run_dir) -> "dict | None":
    return _load_json(run_dir / "performance.json")


def all_choices(run_dir) -> dict:
    """Per-stage records, authoritative ``provider_choices.json`` first."""
    merged = legacy_choices(run_dir)
    merged.update(run_provider_choices(run_dir))
    return merged


def entries(root=None, min_age_days: float = 0.0, include_young: bool = False,
            stage_filter: "str | None" = None):
    """Yield one normalized record per (run, stage) with analytics attached.

    ``min_age_days`` drops runs younger than the given age from the rollup (their
    analytics are meaningless — a just-published watch page is still viral-0);
    ``include_young`` counts them anyway. Runs with no analytics at all surface
    with ``analytics=None`` so the report shows "awaiting data" instead of lying.
    """
    for run_dir in iter_run_dirs(root):
        age = published_age_days(run_dir) or 0.0
        if age < min_age_days and not include_young:
            continue
        perf = performance(run_dir)
        views = perf.get("views") if perf and isinstance(perf.get("views"), int) else None
        avgret = perf.get("avg_view_duration_s") if perf else None
        base = {
            "run_id": run_dir.name,
            "age_days": round(age, 2),
            "views": views,
            "avg_view_duration_s": avgret,
            "analytics": None if perf is None else perf.get("status", "ok"),
        }
        for stage, rec in all_choices(run_dir).items():
            if stage_filter and stage != stage_filter:
                continue
            row = dict(base)
            row.update(rec)
            row["stage"] = stage
            row["provider"] = rec.get("provider") or "?"
            yield row


def rollup(records) -> list:
    """Aggregate per (stage, provider): n, with-analytics, views, retention.

    Pure over ``records`` (so tests feed tiny/fake/none records); never raises on
    missing analytics — those runs count in ``awaiting_data``.
    """
    groups: dict = {}
    for r in records:
        key = (r["stage"], r["provider"])
        groups.setdefault(key, []).append(r)
    rows = []
    for key in sorted(groups):
        rs = groups[key]
        with_views = [r for r in rs if r.get("views") is not None]
        views = [r["views"] for r in with_views]
        rets = [r.get("avg_view_duration_s") for r in with_views
                if r.get("avg_view_duration_s") is not None]
        rows.append({
            "stage": key[0],
            "provider": key[1],
            "n": len(rs),
            "with_analytics": len(with_views),
            "awaiting_data": len(rs) - len(with_views),
            "total_views": sum(views) if views else 0,
            "mean_views": round(statistics.mean(views), 1) if views else None,
            "median_views": round(statistics.median(views), 1) if views else None,
            "mean_retention_s": round(statistics.mean(rets), 1) if rets else None,
        })
    return rows


def print_report(rows: list, min_views: int = 0, min_age_days: float = 0.0) -> None:
    """Pretty-print a rollup; drops nothing (no data shows as 'awaiting')."""
    shown = [r for r in rows
             if r["total_views"] >= max(0, min_views) or r["n"] == 0]
    if not shown:
        print("No provider evidence yet. Publish a few runs (or run "
              "'ab-results --fetch' once they're older than the min age).")
        return
    print(f"Provider performance by stage (videos at least {min_age_days} days old).")
    print(f"  {'stage':<8} {'provider':<12} {'n':>3} {'with':>5} {'await':>6} "
          f"{'views':>9} {'mean':>9} {'median':>9} {'ret(avg, s)':>11}")
    for r in shown:
        mv = "-" if r["mean_views"] is None else f"{r['mean_views']:.1f}"
        med = "-" if r["median_views"] is None else f"{r['median_views']:.1f}"
        ret = "-" if r["mean_retention_s"] is None else f"{r['mean_retention_s']:.1f}"
        print(f"  {r['stage']:<8} {r['provider']:<12} {r['n']:>3} "
              f"{r['with_analytics']:>5} {r['awaiting_data']:>6} "
              f"{r['total_views']:>9} {mv:>9} {med:>9} {ret:>11}")


def fetch_performance(root=None, session=None, min_age_days: float = 0.0,
                      limit: int = 0) -> list:
    """Pull Studio analytics for published runs with no perf file yet.

    Returns one record per attempted run: ``fetched`` (ok), ``no_data`` (too
    young / not yet indexed), ``skipped`` (no logged-in session or no video id),
    ``retired`` (a no_data run that exhausted its retry budget), ``error``. A run
    whose video is too recent has no analytics, which is the
    expected outcome — recorded, never fatal. A ``no_data`` record is NOT a
    lock: Studio may simply have had nothing to render at that moment, so a
    later invocation re-attempts it rather than silently leaving the run
    without analytics forever. Retries are bounded by ``_NO_DATA_RETRY_CAP`` so
    a run whose recorded video id never produces our analytics (stray id, other
    channel) is retired instead of re-scraped on every pass.
    """
    root = Path(root) if root else config.OUTPUT_DIR
    out: list = []
    attempts = 0
    for run_dir in iter_run_dirs(root):
        if limit and attempts >= limit:
            break
        vid = video_id(run_dir)
        if not vid:
            continue
        perf = performance(run_dir)
        # Only a run with real analytics is final. A persisted no_data record
        # (Studio had nothing yet — e.g. a slow render or still-unindexed video)
        # stays retryable so the real numbers can be captured on a later pass.
        if perf is not None and perf.get("status") != "no_data":
            continue
        # A persisted no_data file implies at least one prior fetch pass.
        prev_attempts = int(perf.get("attempts") or 1) if perf is not None else 0
        if prev_attempts >= _NO_DATA_RETRY_CAP:
            out.append({"run_id": run_dir.name, "status": "retired"})
            continue
        age = published_age_days(run_dir) or 0.0
        if age < min_age_days:
            continue
        if session is None:
            out.append({"run_id": run_dir.name, "status": "skipped",
                        "reason": "no logged-in session"})
            continue
        page = session.first_page()
        result = studio_metrics.fetch_metrics(page, vid)
        result["attempts"] = prev_attempts + 1
        (Path(run_dir) / "performance.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        attempts += 1
        out.append({"run_id": run_dir.name,
                    "status": "ok" if result.get("views") is not None else "no_data",
                    "views": result.get("views")})
        log.info("Analytics for %s: %s", run_dir.name, out[-1])
    return out
