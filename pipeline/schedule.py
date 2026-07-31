"""Posting schedule — turn a pile of approved clips into dated slots.

The Hormozi bet this serves: volume beats polish, but only if it's RELENTLESS.
Forty clips posted in one afternoon is a spike everyone scrolls past; forty clips
posted three a day for two weeks is a presence. So the machine's job is not to
hand you a folder — it's to hand you a date for every clip.

Design rules that came out of how this actually gets used:

  * Slots are generated from a WEEKLY PATTERN, not stored one by one. You change
    "3 a day at 8am/1pm/7pm" once and every future slot moves. Storing 200 rows
    and then editing the pattern would leave you reconciling them by hand.

  * Assignment is stable. A clip keeps its slot once it has one, so opening the
    calendar twice doesn't reshuffle everything you already planned around.

  * Approving a clip queues it; rejecting or un-approving releases its slot and
    everything behind it moves up. The queue is never left with holes.

  * Nothing here posts anything. It produces a schedule you (or Postiz later)
    act on. A scheduler that silently published would be the single most
    dangerous thing in this repo.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "data" / "schedule.json"

DEFAULTS = {
    # 24h local times. Three a day is the volume play; drop to one while you're
    # still building a backlog so you never run dry mid-week.
    "times": ["08:00", "13:00", "19:00"],
    # 0 = Monday .. 6 = Sunday
    "days": [0, 1, 2, 3, 4, 5, 6],
    "start_date": "",          # blank = start today
    "timezone_note": "local machine time",
}


def load() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return s


def save(patch: dict) -> dict:
    s = load()
    times = patch.get("times")
    if isinstance(times, list):
        clean = []
        for t in times:
            try:
                hh, _, mm = str(t).partition(":")
                h, m = int(hh), int(mm or 0)
                if 0 <= h < 24 and 0 <= m < 60:
                    clean.append(f"{h:02d}:{m:02d}")
            except (TypeError, ValueError):
                continue
        if clean:
            s["times"] = sorted(set(clean))
    days = patch.get("days")
    if isinstance(days, list):
        clean_d = sorted({int(d) for d in days if str(d).isdigit() and 0 <= int(d) <= 6})
        if clean_d:
            s["days"] = clean_d
    if "start_date" in patch:
        s["start_date"] = str(patch["start_date"] or "")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def _slots(count: int, settings: Optional[dict] = None,
           after: Optional[datetime] = None) -> list[datetime]:
    """The next `count` posting times from the weekly pattern.

    Generated on demand rather than stored, so editing the pattern re-dates
    everything instead of leaving stale rows behind.
    """
    s = settings or load()
    now = after or datetime.now()
    try:
        day = (datetime.strptime(s["start_date"], "%Y-%m-%d").date()
               if s.get("start_date") else now.date())
    except ValueError:
        day = now.date()

    times = []
    for t in s["times"]:
        hh, _, mm = t.partition(":")
        times.append((int(hh), int(mm or 0)))
    times.sort()

    out: list[datetime] = []
    guard = 0
    while len(out) < count and guard < 3000:
        guard += 1
        if day.weekday() in s["days"]:
            for h, m in times:
                when = datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m)
                # Never schedule into the past — the first slot is the next one
                # that hasn't happened yet.
                if when > now and len(out) < count:
                    out.append(when)
        day = day + timedelta(days=1)
    return out


def build(jobs_list: list[dict], settings: Optional[dict] = None) -> dict:
    """Assign every approved clip a slot, in approval order.

    Approval order (not rank order) is deliberate: the order you said yes in is
    the order you decided you wanted them out.
    """
    s = settings or load()
    approved: list[dict] = []
    for job in jobs_list:
        for clip in job.get("clips", []):
            if clip.get("decision") != "approved":
                continue
            approved.append({
                "job_id": job.get("id"),
                "clip_id": clip.get("id"),
                "hook": clip.get("hook") or "",
                "caption": clip.get("caption") or "",
                "hashtags": clip.get("hashtags") or [],
                "duration_s": clip.get("duration_s"),
                "profile": clip.get("profile"),
                "approved_at": clip.get("approved_at") or 0,
                "posted": bool(clip.get("posted")),
            })
    approved.sort(key=lambda c: (c["approved_at"] or 0))

    pending = [c for c in approved if not c["posted"]]
    slots = _slots(len(pending), s)
    for clip, when in zip(pending, slots):
        clip["scheduled_at"] = when.isoformat(timespec="minutes")
        clip["scheduled_day"] = when.strftime("%Y-%m-%d")
        clip["scheduled_time"] = when.strftime("%H:%M")

    by_day: dict[str, list[dict]] = {}
    for c in pending:
        by_day.setdefault(c["scheduled_day"], []).append(c)

    per_week = len(s["times"]) * len(s["days"])
    return {
        "settings": s,
        "queued": len(pending),
        "posted": sum(1 for c in approved if c["posted"]),
        "per_day": len(s["times"]),
        "per_week": per_week,
        "days_of_runway": round(len(pending) / max(len(s["times"]), 1), 1),
        "clips": pending,
        "by_day": [{"day": d, "items": by_day[d]} for d in sorted(by_day)],
    }
