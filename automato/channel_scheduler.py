"""Channel-scheduling layer: decide which channels get a video each day.

Given a daily run budget (e.g. 2), this module picks which channels to publish to
based on *staleness*: the longer a channel has gone without a new video, the
higher its selection weight. A small random component prevents perfectly
deterministic ordering, and a minimum weight floor for high-tier channels
ensures finance/health channels can't drift into silence.

The module is pure: it reads run history from the filesystem, computes weights,
and records the day's plan to an audit trail. It does NOT start any runs —
that's the orchestrator's job.

Exposes:
  * ``upload_history(root)``   -- scan run dirs for per-channel last-upload timestamps
  * ``compute_weights(...)``   -- staleness + tier floor + jitter -> effective weight
  * ``select_channels_for_day(...)``
  * ``print_day_plan(...)``    -- CLI-friendly dry-run output
  * ``record_day_plan(...)``   -- write audit JSON

Environment overrides (re-read per call):
  * ``AUTOMATO_SCHEDULER_BUDGET``       -- daily run count (default 2)
  * ``AUTOMATO_SCHEDULER_HIGH_TIER_FLOOR`` -- min weight for high-tier (default 2.0)
  * ``AUTOMATO_SCHEDULER_RANDOM_RANGE`` -- max jitter added (default 0.5)
  * ``AUTOMATO_SCHEDULER_NEVER_UPLOADED_STALENESS`` -- days assumed for a
    channel with zero history (default 30.0)
  * ``AUTOMATO_SCHEDULER_SEED`` -- int override for deterministic jitter (tests)
"""
from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import config
from .channels import channel_names, risk_tier_for

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration helpers (env overrides, re-read per call)
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _budget() -> int:
    return max(1, _env_int("AUTOMATO_SCHEDULER_BUDGET", 2))


def _high_tier_floor() -> float:
    return max(0.0, _env_float("AUTOMATO_SCHEDULER_HIGH_TIER_FLOOR", 2.0))


def _random_range() -> float:
    return max(0.0, _env_float("AUTOMATO_SCHEDULER_RANDOM_RANGE", 0.5))


def _never_uploaded_staleness() -> float:
    return max(1.0, _env_float("AUTOMATO_SCHEDULER_NEVER_UPLOADED_STALENESS", 30.0))


def _rng(seed: "int | None" = None) -> random.Random:
    """Optionally-seeded RNG for deterministic jitter in tests."""
    env_seed = os.environ.get("AUTOMATO_SCHEDULER_SEED", "").strip()
    if env_seed:
        try:
            return random.Random(int(env_seed))
        except ValueError:
            pass
    if seed is not None:
        return random.Random(seed)
    return random.Random()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ChannelHistory:
    channel: str
    last_upload_epoch: float
    last_upload_run_id: str
    days_since_last: float


@dataclass
class ChannelWeight:
    channel: str
    risk_tier: str
    days_since_last: float
    staleness_weight: float
    tier_floor: float
    random_component: float
    effective_weight: float


@dataclass
class DayPlan:
    date: str
    budget: int
    weights: list[ChannelWeight] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    method: str = "staleness_weighted"


# ---------------------------------------------------------------------------
# Upload history scanner
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> "dict | None":
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _epoch_from_run_id(run_id: str) -> "float | None":
    """Extract the epoch prefix from a run id like '1789629908_4bf726'."""
    try:
        return float(run_id.split("_")[0])
    except (ValueError, IndexError):
        return None


def upload_history(root: "str | Path | None" = None,
                   now: "float | None" = None) -> list[ChannelHistory]:
    """Scan run directories and return per-channel last-upload records.

    Sources (in priority order):
      1. ``upload_attempted.json`` (has ``channel`` + ``attempted_at``)
      2. ``post_url.json`` (has ``channel`` in newer runs)
      3. ``run_state.json`` (has ``seed`` with topic -> channel via keyword map)

    A channel with no upload history at all is absent from the list (callers
    treat it as never-uploaded).
    """
    root = Path(root) if root else config.OUTPUT_DIR
    if not root.is_dir():
        return []

    # channel -> (epoch, run_id)
    last_by_channel: dict[str, tuple[float, str]] = {}

    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        run_id = run_dir.name

        # Source 1: upload_attempted.json (most reliable)
        ua = _load_json(run_dir / "upload_attempted.json")
        if ua and ua.get("channel"):
            ch = ua["channel"]
            epoch = ua.get("attempted_at") or _epoch_from_run_id(run_id)
            if epoch:
                if ch not in last_by_channel or epoch > last_by_channel[ch][0]:
                    last_by_channel[ch] = (float(epoch), run_id)
            continue  # prefer this source; skip fallbacks

        # Source 2: post_url.json with channel field
        pu = _load_json(run_dir / "post_url.json")
        if pu and pu.get("channel"):
            ch = pu["channel"]
            epoch = _epoch_from_run_id(run_id)
            if epoch:
                if ch not in last_by_channel or epoch > last_by_channel[ch][0]:
                    last_by_channel[ch] = (epoch, run_id)
            continue

        # Source 3: run_state.json seed topic -> resolve channel
        rs = _load_json(run_dir / "run_state.json")
        if rs and rs.get("seed", {}).get("topic"):
            from .channels import resolve_channel_name
            ch = resolve_channel_name(rs["seed"]["topic"])
            epoch = rs.get("created_at") or _epoch_from_run_id(run_id)
            if ch and epoch:
                if ch not in last_by_channel or float(epoch) > last_by_channel[ch][0]:
                    last_by_channel[ch] = (float(epoch), run_id)

    if now is None:
        now = time.time()
    result = []
    for ch, (epoch, run_id) in sorted(last_by_channel.items()):
        days = max(0.0, (now - epoch) / 86400.0)
        result.append(ChannelHistory(
            channel=ch,
            last_upload_epoch=epoch,
            last_upload_run_id=run_id,
            days_since_last=round(days, 2),
        ))
    return result


def channels_active_on_date(root: "str | Path | None" = None,
                            target_date: "date | None" = None) -> set:
    """Channels that already have a completed upload attempt on the given day.

    Used by ``select_channels_for_day`` to prevent re-picking a channel when
    a crash-recovery re-run hits the same date.
    """
    target_date = target_date or date.today()
    now = datetime(*target_date.timetuple()[:3]).timestamp()
    hist = upload_history(root, now=now)
    return {h.channel for h in hist if date.fromtimestamp(h.last_upload_epoch) == target_date}


# ---------------------------------------------------------------------------
# Weight computation
# ---------------------------------------------------------------------------

def compute_weights(history: list[ChannelHistory],
                    rng: "random.Random | None" = None) -> list[ChannelWeight]:
    """Compute effective selection weights for all known channels.

    Channels with upload history use their real staleness. Channels with NO
    history (never uploaded) get ``never_uploaded_staleness`` days so they're
    prioritised for a first run.

    Weight formula per channel:
      effective = staleness_weight + tier_floor + random_component

    Where:
      staleness_weight  = min(days_since_last, 30)
      tier_floor        = HIGH_TIER_FLOOR  if risk_tier == "high" else 0
      random_component  = Uniform(0, RANDOM_RANGE)
    """
    if rng is None:
        rng = _rng()
    floor = _high_tier_floor()
    rand_max = _random_range()
    never_stale = _never_uploaded_staleness()

    # Start with channels that have history
    known_channels = {h.channel: h for h in history}

    # All registered channels + channels with history (union)
    all_channels = sorted(set(list(known_channels) + channel_names()))

    weights = []
    for ch in all_channels:
        hist = known_channels.get(ch)
        if hist:
            days = hist.days_since_last
        else:
            days = never_stale  # never uploaded -> high staleness

        staleness = min(days, 30.0)
        tier = risk_tier_for(ch)
        tf = floor if tier == "high" else 0.0
        jitter = rng.uniform(0, rand_max)
        effective = round(staleness + tf + jitter, 4)

        weights.append(ChannelWeight(
            channel=ch,
            risk_tier=tier,
            days_since_last=round(days, 2),
            staleness_weight=round(staleness, 4),
            tier_floor=tf,
            random_component=round(jitter, 4),
            effective_weight=effective,
        ))

    # Sort descending by effective weight (highest first)
    weights.sort(key=lambda w: w.effective_weight, reverse=True)
    return weights


# ---------------------------------------------------------------------------
# Channel selection
# ---------------------------------------------------------------------------

def select_channels_for_day(
    target_date: "date | None" = None,
    budget: "int | None" = None,
    root: "str | Path | None" = None,
    seed: "int | None" = None,
    exclude: "set | None" = None,
) -> DayPlan:
    """Pick which channels get today's videos.

    Uses weighted random sampling without replacement: channels with higher
    effective weights are more likely to be selected, but a low-tier channel
    that has been quiet for 20+ days can still beat a high-tier channel that
    uploaded yesterday.

    ``exclude`` — channels already allocated today (crash-recovery re-run);
    excluded from the candidate pool but still shown in the weight table so
    the plan is auditable.

    Returns a ``DayPlan`` with both the full weight table and the selected
    channels. Does NOT execute any runs.
    """
    if target_date is None:
        target_date = date.today()
    if budget is None:
        budget = _budget()
    exclude = set(exclude or set())

    # Staleness is measured at the *start* of the target day, so planning ahead
    # and schedules re-run on the same date stay deterministic.
    now = (datetime(*target_date.timetuple()[:3]).timestamp()
           if target_date else time.time())
    history = upload_history(root, now=now)
    rng = _rng(seed)
    weights = compute_weights(history, rng)

    if not weights:
        return DayPlan(
            date=target_date.isoformat(),
            budget=budget,
            weights=[],
            selected=[],
            excluded=sorted(exclude),
        )

    # Candidate pool = weights not already used today
    candidates = [w for w in weights if w.channel not in exclude]

    # Weighted random sampling without replacement (from candidates only)
    selected: list[str] = []
    remaining = list(candidates)

    for _ in range(min(budget, len(remaining))):
        total = sum(w.effective_weight for w in remaining)
        if total <= 0:
            break
        r = rng.random() * total
        cumulative = 0.0
        for i, w in enumerate(remaining):
            cumulative += w.effective_weight
            if cumulative > r or i == len(remaining) - 1:
                selected.append(w.channel)
                remaining.pop(i)
                break

    return DayPlan(
        date=target_date.isoformat(),
        budget=budget,
        weights=weights,
        selected=selected,
        excluded=sorted(exclude),
    )


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def _selections_path(root: "str | Path | None" = None) -> Path:
    root = Path(root) if root else config.OUTPUT_DIR
    return root / "channel_selections.json"


def record_day_plan(plan: DayPlan,
                    root: "str | Path | None" = None,
                    runs: "dict | None" = None) -> Path:
    """Append a DayPlan to the audit trail (channel_selections.json).

    The file is a list of day-entries. Each entry records the date, budget,
    per-channel weights, and the actual selection, so the fairness claim is
    auditable: "on 2026-09-17 the scheduler gave es-finance a weight of 32.7
    and selected it; on 2026-09-18 main was selected with weight 15.3."

    ``runs`` maps selected channel -> run outcomes (run_id + status + url),
    appended by ``scheduled-run`` after execution so the day entry shows not
    just the allocation but what actually got delivered.
    """
    path = _selections_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (ValueError, OSError):
        data = []
    if not isinstance(data, list):
        data = []

    entry = {
        "date": plan.date,
        "budget": plan.budget,
        "method": plan.method,
        "weights": [asdict(w) for w in plan.weights],
        "selected": plan.selected,
        "excluded": plan.excluded,
        "recorded_at": time.time(),
    }
    if runs:
        entry["runs"] = runs

    # Replace if same date already exists (idempotent re-runs)
    data = [d for d in data if d.get("date") != plan.date]
    data.append(entry)
    data.sort(key=lambda d: d["date"])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Day plan for %s recorded -> %s", plan.date, path)
    return path


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------

def _topic_hints(channel: str) -> list:
    """The registry's routing keywords for a channel (example topic seeds)."""
    from .channels import profile_for
    return list((profile_for(channel).get("keywords") or []))

def print_day_plan(plan: DayPlan, file=None) -> None:
    """Pretty-print a DayPlan for the dry-run CLI."""
    out = file or sys.stdout

    def write(s: str) -> None:
        print(s, file=out)

    write(f"\nChannel schedule for {plan.date} (budget: {plan.budget})")
    write(f"  method: {plan.method}\n")

    if not plan.weights:
        write("  No channels available (none registered or no upload history).")
        return

    write(f"  {'channel':<22} {'tier':<6} {'days':>6} {'stale':>7} {'floor':>6} "
          f"{'jitter':>7} {'weight':>8}  {'status':>14}")
    write(f"  {'-'*22} {'-'*6} {'-'*6} {'-'*7} {'-'*6} {'-'*7} {'-'*8}  {'-'*14}")

    for w in plan.weights:
        if w.channel in plan.selected:
            status = "* selected"
        elif w.channel in plan.excluded:
            status = "ran today"
        else:
            status = ""
        write(f"  {w.channel:<22} {w.risk_tier:<6} {w.days_since_last:>6.1f} "
              f"{w.staleness_weight:>7.2f} {w.tier_floor:>6.1f} "
              f"{w.random_component:>7.4f} {w.effective_weight:>8.4f}  {status:>14}")

    write(f"\n  Selected: {', '.join(plan.selected) or '(none)'}")
    if plan.excluded:
        write(f"  Skipped (already ran today): {', '.join(plan.excluded)}")
    write("  ( * = selected )")

    # Planned channel->topic assignments (dry-run): the scheduler allocates a
    # channel per slot; the actual topic is derived per-run by Ask Studio
    # ideation (or the topic-keyword map when a topic is given). Show each
    # selected channel's registry keywords as the example seed so the plan reads
    # as channel+topic rather than bare channel names.
    if plan.selected:
        write("\n  Planned assignments (topic derived per run; keywords = example seed):")
        for ch in plan.selected:
            hints = _topic_hints(ch)
            write(f"    * {ch:<22} -> topic via ideation"
                  + (f" (keywords: {', '.join(hints)})" if hints else ""))
