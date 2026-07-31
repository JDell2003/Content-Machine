"""Rolling storage policy.

    raw source video   -> deleted after 48 hours
    rendered clips     -> deleted after 7 days
    approved clips     -> kept forever
    transcripts        -> kept (few MB, and they make re-runs free)
    job records        -> kept (small; they hold hooks, captions, scores)

Rationale: by 48h the clips are cut, and by 7 days anything worth posting is on
Instagram, which is now the copy of record. Keeping transcripts means a re-run
with tuned ranking still works after the source is gone — it just can't re-render
video, which is the correct trade.

Runs on server start and every 6 hours after. Every deletion is logged so nothing
vanishes silently.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from server import config

SOURCE_MAX_AGE_H = 48
CLIP_MAX_AGE_DAYS = 7
LOG = config.LOGS / "retention.jsonl"


def _log(entry: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **entry}) + "\n")
    except OSError:
        pass


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _approved_names() -> set[str]:
    """Approved copies live in approved/ under their own names; never delete a
    clip whose approved twin exists, and never touch approved/ itself."""
    return {p.name for p in config.APPROVED.glob("*") if p.is_file()}


def sweep(dry_run: bool = False) -> dict:
    now = time.time()
    freed = 0
    removed = {"sources": [], "clip_dirs": []}

    # ---- raw sources older than 48h -------------------------------------
    src_cutoff = now - SOURCE_MAX_AGE_H * 3600
    for p in config.UPLOADS.glob("*"):
        if not p.is_file() or p.suffix.lower() not in config.VIDEO_SUFFIXES:
            continue  # leave transcripts alone
        if p.stat().st_mtime >= src_cutoff:
            continue
        size = p.stat().st_size
        if not dry_run:
            try:
                p.unlink()
            except OSError:
                continue
        freed += size
        removed["sources"].append({"name": p.name, "gb": round(size / 1e9, 2)})

    # ---- rendered clips older than 7 days --------------------------------
    clip_cutoff = now - CLIP_MAX_AGE_DAYS * 86400
    approved = _approved_names()
    for job_dir in config.CLIPS.glob("*"):
        if not job_dir.is_dir():
            continue
        if job_dir.stat().st_mtime >= clip_cutoff:
            continue
        # Never remove a job that still holds a file we approved.
        if any(f.name in approved for f in job_dir.rglob("*") if f.is_file()):
            continue
        size = _size(job_dir)
        if not dry_run:
            shutil.rmtree(job_dir, ignore_errors=True)
        freed += size
        removed["clip_dirs"].append({"job": job_dir.name, "gb": round(size / 1e9, 2)})

    result = {"dry_run": dry_run, "freed_gb": round(freed / 1e9, 2), **removed}
    if freed or dry_run:
        _log(result)
    return result


def usage() -> dict:
    """What's on disk right now, for the storage view."""
    out = {}
    for label, path in (("sources", config.UPLOADS), ("clips", config.CLIPS),
                        ("approved", config.APPROVED), ("tmp", config.TMP),
                        ("models", config.MODELS)):
        try:
            out[label + "_gb"] = round(_size(path) / 1e9, 2)
        except OSError:
            out[label + "_gb"] = 0.0
    try:
        du = shutil.disk_usage(str(config.ROOT))
        out["disk_free_gb"] = round(du.free / 1e9, 1)
        out["disk_total_gb"] = round(du.total / 1e9, 1)
    except OSError:
        pass
    out["policy"] = {"source_hours": SOURCE_MAX_AGE_H,
                     "clip_days": CLIP_MAX_AGE_DAYS,
                     "approved": "kept forever"}
    return out
