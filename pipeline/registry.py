"""Post registry — what went out, when, and how it did.

This is the memory that makes week three's decision (contract onto winners, or
push to 3/day) a reading rather than a guess. Without it, `tune` only ever learns
from what you REJECTED — taste, but not results.

Three things get recorded, in one append-only file:

    published   a clip went out: platform, time, slot, and the platform's own
                media id so metrics can be pulled later
    metrics     views/likes/saves/comments/shares at some point after posting
    (both)      keyed to job_id + clip_id, so everything joins back to the clip,
                its source video, its topic, and the slot it went out in

APPEND-ONLY on purpose. Metrics get pulled repeatedly as a post matures, and the
latest reading for a post wins — but the earlier ones stay, because "3k views in
hour one" and "3k views in week two" are completely different signals and
overwriting would destroy that.

Nothing here publishes. It records what already happened.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "performance.jsonl"

METRIC_KEYS = ("views", "likes", "saves", "comments", "shares", "reach", "watch_pct")


def _append(row: dict) -> dict:
    row["ts"] = time.time()
    POSTS.parent.mkdir(parents=True, exist_ok=True)
    with POSTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def rows() -> list[dict]:
    if not POSTS.exists():
        return []
    out = []
    for line in POSTS.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def record_published(*, job_id: str, clip_id: str, platform: str = "instagram",
                     media_id: str = "", slot_at: str = "", permalink: str = "",
                     hook: str = "", source_name: str = "", topic: str = "",
                     profile: str = "") -> dict:
    """Called at publish time. `media_id` is the platform's id — the handle every
    later metrics pull needs, so it's captured now rather than reconstructed."""
    return _append({
        "kind": "published", "job_id": job_id, "clip_id": clip_id,
        "platform": platform, "media_id": media_id, "permalink": permalink,
        "slot_at": slot_at, "hook": hook, "source_name": source_name,
        "topic": topic, "profile": profile,
    })


def record_metrics(*, job_id: str, clip_id: str, **metrics) -> dict:
    clean = {k: float(v) for k, v in metrics.items()
             if k in METRIC_KEYS and v not in (None, "")}
    return _append({"kind": "metrics", "job_id": job_id, "clip_id": clip_id, **clean})


def _key(r: dict) -> str:
    return f"{r.get('job_id')}|{r.get('clip_id')}"


def posts() -> list[dict]:
    """One row per published clip, with its LATEST metrics merged in."""
    pub: dict[str, dict] = {}
    latest: dict[str, dict] = {}
    for r in rows():
        k = _key(r)
        if r.get("kind") == "published":
            pub[k] = r
        elif r.get("kind") == "metrics":
            # Latest reading wins; earlier ones stay on disk as history.
            prev = latest.get(k, {})
            latest[k] = {**prev, **{m: r[m] for m in METRIC_KEYS if m in r},
                         "measured_ts": r.get("ts")}
    out = []
    for k, p in pub.items():
        out.append({**p, **latest.get(k, {}), "has_metrics": k in latest})
    out.sort(key=lambda r: -(r.get("ts") or 0))
    return out


def _avg(vals: list[float]) -> float:
    return round(sum(vals) / len(vals), 1) if vals else 0.0


def patterns(metric: str = "views") -> dict:
    """What actually performs, cut the three ways that change a decision.

    Deliberately reports the SAMPLE SIZE next to every average. With four posts,
    "6pm outperforms 11am by 40%" is noise, and a dashboard that renders it as a
    finding will get you optimising against randomness.
    """
    have = [p for p in posts() if p.get("has_metrics") and p.get(metric) is not None]

    by_slot: dict[str, list[float]] = defaultdict(list)
    by_source: dict[str, list[float]] = defaultdict(list)
    by_topic: dict[str, list[float]] = defaultdict(list)
    by_weekday: dict[str, list[float]] = defaultdict(list)

    for p in have:
        v = float(p[metric])
        at = str(p.get("slot_at") or "")
        if len(at) >= 16:
            by_slot[at[11:16]].append(v)
            try:
                import datetime as _dt  # noqa: PLC0415
                d = _dt.datetime.fromisoformat(at)
                by_weekday[d.strftime("%a")].append(v)
            except ValueError:
                pass
        by_source[p.get("source_name") or "?"].append(v)
        by_topic[p.get("topic") or p.get("profile") or "?"].append(v)

    def rank(d: dict[str, list[float]]) -> list[dict]:
        return sorted(
            [{"key": k, "avg": _avg(v), "n": len(v)} for k, v in d.items()],
            key=lambda r: -r["avg"])

    n = len(have)
    return {
        "metric": metric,
        "posts_with_metrics": n,
        # The honest gate. Under this, differences are noise and the UI says so
        # instead of drawing conclusions from four data points.
        "enough_data": n >= 12,
        "needed": max(0, 12 - n),
        "by_slot": rank(by_slot),
        "by_weekday": rank(by_weekday),
        "by_source": rank(by_source),
        "by_topic": rank(by_topic),
        "total_published": len(posts()),
    }
