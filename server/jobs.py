"""Job store + single-slot worker.

One video at a time, on purpose: whisper and ffmpeg will each happily eat every
core, and two videos in parallel is slower than two in sequence. Jobs are plain
JSON on disk so a crash (or a reboot mid-render) never loses the queue, and so
the status page can be a dumb reader.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import config

_LOCK = threading.RLock()
_WORKER: Optional[threading.Thread] = None
_RUNNER: Optional[Callable[[dict, Callable[..., None]], None]] = None

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
ACTIVE_STATES = {QUEUED, RUNNING}


def _path(job_id: str) -> Path:
    return config.JOBS / f"{job_id}.json"


def _write(job: dict) -> None:
    # Write-then-rename: the status page polls constantly and must never read a
    # half-written file.
    p = _path(job["id"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    tmp.replace(p)


def load(job_id: str) -> Optional[dict]:
    p = _path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def all_jobs() -> list[dict]:
    out = []
    for p in config.JOBS.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda j: j.get("created_at", 0), reverse=True)
    return out


def create(source_path: Path, original_name: str, profile: str = "BRAND",
           include_raw: bool = False) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "profile": str(profile or "BRAND").upper(),
        "include_raw": bool(include_raw),
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "status": QUEUED,
        "stage": "queued",
        "progress": 0.0,
        "message": "Waiting for the machine to free up",
        "source_path": str(source_path),
        "original_name": original_name,
        "size_bytes": source_path.stat().st_size if source_path.exists() else 0,
        "duration_s": None,
        "clips": [],
        "brain_calls": [],
        "error": None,
    }
    with _LOCK:
        _write(job)
    _ensure_worker()
    return job


def update(job_id: str, **fields: Any) -> Optional[dict]:
    with _LOCK:
        job = load(job_id)
        if not job:
            return None
        job.update(fields)
        _write(job)
        return job


def append(job_id: str, key: str, value: Any) -> Optional[dict]:
    with _LOCK:
        job = load(job_id)
        if not job:
            return None
        job.setdefault(key, []).append(value)
        _write(job)
        return job


def cancel(job_id: str) -> Optional[dict]:
    """Cancel only helps a queued job; a running ffmpeg/whisper is left alone
    rather than half-killed, so the flag is advisory for the worker loop."""
    with _LOCK:
        job = load(job_id)
        if not job:
            return None
        if job["status"] == QUEUED:
            job.update(status=CANCELLED, stage="cancelled", message="Cancelled before it started",
                       finished_at=time.time())
            _write(job)
        else:
            job["cancel_requested"] = True
            _write(job)
        return job


def set_runner(fn: Callable[[dict, Callable[..., None]], None]) -> None:
    global _RUNNER
    _RUNNER = fn


def _next_queued() -> Optional[dict]:
    with _LOCK:
        pending = [j for j in all_jobs() if j.get("status") == QUEUED]
        pending.sort(key=lambda j: j.get("created_at", 0))
        return pending[0] if pending else None


def _log(job_id: str, text: str) -> None:
    try:
        with (config.LOGS / f"{job_id}.log").open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")
    except OSError:
        pass


def _worker_loop() -> None:
    while True:
        job = _next_queued()
        if not job:
            time.sleep(1.5)
            continue
        job_id = job["id"]
        update(job_id, status=RUNNING, started_at=time.time(), stage="starting",
               progress=0.01, message="Starting")
        _log(job_id, f"start {job.get('original_name')}")

        def report(stage: str = None, progress: float = None, message: str = None, **extra):
            fields = {k: v for k, v in extra.items()}
            if stage is not None:
                fields["stage"] = stage
            if progress is not None:
                fields["progress"] = max(0.0, min(1.0, float(progress)))
            if message is not None:
                fields["message"] = message
                _log(job_id, f"{stage or ''} {message}".strip())
            return update(job_id, **fields)

        try:
            if _RUNNER is None:
                raise RuntimeError("No pipeline runner registered")
            _RUNNER(load(job_id) or job, report)
            fresh = load(job_id) or {}
            if fresh.get("status") == RUNNING:
                update(job_id, status=DONE, stage="done", progress=1.0,
                       finished_at=time.time(), message="Ready for review")
            _log(job_id, "done")
        except Exception as exc:  # noqa: BLE001 - a bad video must not kill the queue
            detail = traceback.format_exc(limit=6)
            _log(job_id, f"FAILED {exc}\n{detail}")
            update(job_id, status=FAILED, stage="failed", finished_at=time.time(),
                   message=f"Failed: {exc}", error=detail[-4000:])


def _ensure_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(target=_worker_loop, name="cm-worker", daemon=True)
        _WORKER.start()


def start_worker() -> None:
    # Anything left RUNNING is a corpse from a previous process; re-queue it so a
    # reboot mid-job doesn't silently drop the video.
    for job in all_jobs():
        if job.get("status") == RUNNING:
            update(job["id"], status=QUEUED, stage="queued", progress=0.0,
                   message="Re-queued after a restart")
    _ensure_worker()
