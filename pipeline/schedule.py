"""Posting calendar with a cadence GOVERNOR.

Postiz (when it exists) does the publishing. This is the strategy layer that
decides what goes out when — and, more importantly, what does NOT.

THE DOCTRINE, IN CODE
---------------------
The cadence is a CEILING, not a quota. Two rules follow from that, and they are
the whole point of this module:

  1. SURPLUS EXTENDS.  Approve 20 clips in one sitting and the queue runs
     forward into future days. It never floods today to clear a backlog.
     Volume never outruns judgement.

  2. SCARCITY PASSES.  A slot with no approved clip stays empty. There is no
     backfill, no "1 slot open" nudge, no badge, no suggestion to lower the bar
     to fill a hole. An empty slot is a normal outcome, not a failure state.

Rule 2 is the one that's easy to erode, because "you have an empty slot" feels
like helpful UI. It isn't — it's pressure to approve something you already
decided against. Anything that counts, highlights, or colours empty slots as a
deficit belongs nowhere near this file. `build()` deliberately returns no
"unfilled" count for the UI to render as a warning.

VARIETY GUARD
-------------
A three-hour Sunday meeting should not post as three consecutive days of the
same conversation. Auto-assignment interleaves across source videos and
profiles, and adjacent same-source slots are WARNED, never blocked — you might
genuinely want a two-parter.

PHASE DIAL
----------
One setting, "expansion" or "contraction", so week three's decision (push to
3/day, or cut back onto winners) is a toggle rather than a rebuild.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "data" / "schedule.json"

PHASES = {
    # per_day is the CEILING for that phase.
    "contraction": {"label": "Contraction — fewer, onto winners", "per_day": 1},
    "expansion": {"label": "Expansion — steady volume", "per_day": 2},
    "expansion_hard": {"label": "Expansion+ — 3/day", "per_day": 3},
}

DEFAULTS = {
    "phase": "expansion",
    "slots": ["11:30", "18:30"],     # local machine time
    "days": [0, 1, 2, 3, 4, 5, 6],   # 0 = Monday
    "min_gap_hours": 5,
    "start_date": "",
    "per_day_override": None,        # None = take it from the phase
}


def load() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    if s.get("phase") not in PHASES:
        s["phase"] = "expansion"
    return s


def per_day(s: Optional[dict] = None) -> int:
    s = s or load()
    if s.get("per_day_override"):
        return max(1, int(s["per_day_override"]))
    return PHASES[s["phase"]]["per_day"]


def save(patch: dict) -> dict:
    s = load()
    slots = patch.get("slots")
    if isinstance(slots, list):
        clean = []
        for t in slots:
            try:
                hh, _, mm = str(t).partition(":")
                h, m = int(hh), int(mm or 0)
                if 0 <= h < 24 and 0 <= m < 60:
                    clean.append(f"{h:02d}:{m:02d}")
            except (TypeError, ValueError):
                continue
        if clean:
            s["slots"] = sorted(set(clean))
    days = patch.get("days")
    if isinstance(days, list):
        d = sorted({int(x) for x in days if str(x).isdigit() and 0 <= int(x) <= 6})
        if d:
            s["days"] = d
    if patch.get("phase") in PHASES:
        s["phase"] = patch["phase"]
    if "min_gap_hours" in patch:
        try:
            s["min_gap_hours"] = max(0, min(24, float(patch["min_gap_hours"])))
        except (TypeError, ValueError):
            pass
    if "per_day_override" in patch:
        v = patch["per_day_override"]
        s["per_day_override"] = None if v in (None, "", 0) else max(1, int(v))
    if "start_date" in patch:
        s["start_date"] = str(patch["start_date"] or "")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def slot_times(days_ahead: int = 14, s: Optional[dict] = None,
               now: Optional[datetime] = None) -> list[datetime]:
    """Every slot in the window, honouring the per-day CEILING and the min gap.

    Two independent limiters, both applied here so no caller can bypass them:
      * per_day    caps how many slots a single day can ever offer
      * min_gap    drops a slot that lands too close behind the one before it,
                   which is what stops "nothing posts same-hour"
    """
    s = s or load()
    now = now or datetime.now()
    cap = per_day(s)
    gap = timedelta(hours=float(s.get("min_gap_hours") or 0))

    try:
        day = (datetime.strptime(s["start_date"], "%Y-%m-%d").date()
               if s.get("start_date") else now.date())
    except ValueError:
        day = now.date()

    parsed = []
    for t in s["slots"]:
        hh, _, mm = t.partition(":")
        parsed.append((int(hh), int(mm or 0)))
    parsed.sort()

    out: list[datetime] = []
    last: Optional[datetime] = None
    for i in range(days_ahead + 1):
        d = day + timedelta(days=i)
        if d.weekday() not in s["days"]:
            continue
        used_today = 0
        for h, m in parsed:
            if used_today >= cap:
                break                       # the ceiling, per day
            when = datetime.combine(d, datetime.min.time()).replace(hour=h, minute=m)
            if when <= now:
                continue                    # never schedule into the past
            if last is not None and when - last < gap:
                continue                    # too close behind the previous post
            out.append(when)
            last = when
            used_today += 1
    return out


def _interleave(clips: list[dict]) -> list[dict]:
    """Round-robin across source videos so one long recording can't own a week.

    Preserves approval order WITHIN each source, so the order you said yes in
    still shows through — it's the sources that take turns, not the clips.
    """
    buckets: dict[str, list[dict]] = {}
    for c in clips:
        buckets.setdefault(f"{c.get('job_id')}|{c.get('profile')}", []).append(c)
    for b in buckets.values():
        b.sort(key=lambda c: c.get("approved_at") or 0)

    out: list[dict] = []
    while any(buckets.values()):
        for key in sorted(buckets):
            if buckets[key]:
                out.append(buckets[key].pop(0))
    return out


def build(jobs_list: list[dict], days_ahead: int = 14,
          s: Optional[dict] = None) -> dict:
    s = s or load()

    approved: list[dict] = []
    for job in jobs_list:
        for clip in job.get("clips", []):
            if clip.get("decision") != "approved" or clip.get("posted"):
                continue
            approved.append({
                "job_id": job.get("id"), "clip_id": clip.get("id"),
                "hook": clip.get("hook") or "", "caption": clip.get("caption") or "",
                "hashtags": clip.get("hashtags") or [],
                "duration_s": clip.get("duration_s"), "profile": clip.get("profile"),
                "topic": clip.get("topic"), "source_name": job.get("original_name"),
                "approved_at": clip.get("approved_at") or 0,
                "pinned_at": clip.get("scheduled_at"),   # manual override wins
            })

    # A clip you dragged somewhere keeps that slot; everything else flows.
    pinned = [c for c in approved if c["pinned_at"]]
    flowing = _interleave([c for c in approved if not c["pinned_at"]])

    times = slot_times(days_ahead, s)
    taken = {c["pinned_at"] for c in pinned}
    open_times = [t for t in times if t.isoformat(timespec="minutes") not in taken]

    for clip, when in zip(flowing, open_times):
        clip["scheduled_at"] = when.isoformat(timespec="minutes")
    for c in pinned:
        c["scheduled_at"] = c["pinned_at"]

    placed = [c for c in pinned + flowing if c.get("scheduled_at")]
    placed.sort(key=lambda c: c["scheduled_at"])

    # Variety guard: warn, never block. You might want a deliberate two-parter.
    warnings = []
    for a, b in zip(placed, placed[1:]):
        if a["job_id"] == b["job_id"]:
            warnings.append({"clip_id": b["clip_id"],
                             "message": f"back-to-back from {b['source_name'] or 'the same video'}"})

    by_slot = {c["scheduled_at"]: c for c in placed}
    calendar = []
    for t in times:
        key = t.isoformat(timespec="minutes")
        calendar.append({
            "at": key, "day": t.strftime("%Y-%m-%d"), "time": t.strftime("%H:%M"),
            "clip": by_slot.get(key),
        })

    # Clips approved but with nowhere to go inside the window. This is a QUEUE
    # DEPTH figure, not a backlog to clear — it means you're ahead, which is the
    # healthy direction.
    overflow = len(pinned) + len(flowing) - len(placed)

    return {
        "settings": s,
        "phase": s["phase"],
        "phase_label": PHASES[s["phase"]]["label"],
        "per_day": per_day(s),
        "min_gap_hours": s.get("min_gap_hours"),
        "queued": len(placed),
        "beyond_window": max(0, overflow),
        "days_of_runway": round(len(placed) / max(per_day(s), 1), 1),
        # The ceiling cannot invent a slot. Flipping to 3/day with only two times
        # defined silently delivers 2/day, so say so instead of under-delivering
        # quietly — this is about the dial telling the truth, not about filling
        # anything.
        "capacity_note": (
            f"Phase allows {per_day(s)}/day but only {len(s['slots'])} slot "
            f"time{'s are' if len(s['slots']) != 1 else ' is'} set, so the real "
            f"ceiling is {len(s['slots'])}/day. Add a time to reach {per_day(s)}."
            if per_day(s) > len(s["slots"]) else ""),
        "calendar": calendar,
        "warnings": warnings,
        # Deliberately absent: any count of empty slots. An empty slot is not a
        # deficit and must never be rendered as one. See the module docstring.
    }
