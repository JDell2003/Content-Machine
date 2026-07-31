"""Re-grade and re-voice clips AFTER you've seen them.

The mistake this fixes: look and sound used to be a guess made before ranking,
which meant a wrong guess produced sixty finished clips that were all subtly
wrong at once. You cannot judge a grade from a preset name — you judge it by
looking at your own footage.

So the order is now: render with the default, LOOK at it, then adjust.

QUALITY: re-rendering goes back to the ORIGINAL SOURCE and re-cuts, exactly like
the first render did. That is one encode from source, not a second generation on
top of the finished clip — the same rule the whole renderer is built around.

That only holds while the source is still on disk (48h retention). Past that, the
finished clip is all that's left and a re-grade would stack a second generation.
`plan()` reports which case you're in so the UI can say so rather than quietly
degrading the clip.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import audio_fx, captions, color, outro, render


def source_for(job: dict) -> Optional[Path]:
    raw = job.get("source_path") or job.get("path")
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


def select(job: dict, scope: str = "all") -> list[dict]:
    """Which clips a batch should touch.

    This is the only lever with the right order of magnitude. MEASURED on real
    clips: 1.80x realtime aggregate, so 131 minutes of footage is ~73 minutes of
    re-rendering no matter how the encoder is tuned. Re-grading 20 kept clips
    instead of all 60 is a 3x saving that costs nothing, because the clips you
    rejected are never going to be posted.
    """
    clips = [c for c in job.get("clips", []) if c.get("path")]
    if scope == "approved":
        return [c for c in clips if c.get("decision") == "approved"]
    if scope == "keeping":
        return [c for c in clips if c.get("decision") != "rejected"]
    return clips


def plan(job: dict, scope: str = "all") -> dict:
    """Can we re-render losslessly, or only re-encode what's left?"""
    src = source_for(job)
    clips = select(job, scope)
    return {
        "clips": len(clips),
        "lossless": src is not None,
        "source": str(src) if src else None,
        "note": ("Re-cuts from the original recording — same quality as the first render."
                 if src else
                 "The source recording has been swept (48h retention). Re-grading now "
                 "would re-encode the finished clips, costing one generation of quality."),
        "eta_minutes": eta_minutes(clips),
    }


# MEASURED, not guessed: a 148.9s 4K portrait clip took 277s end to end
# (seek + lossless pre-cut + two-pass loudness analysis + NVENC encode).
# That is ~1.9x realtime per clip on one worker. An early version of this file
# claimed 45s/clip — the figure from the ORIGINAL render, which was only that
# fast because it ran three clips in parallel. Quoting it here would have told
# you "45 minutes" for a job that actually takes over four hours.
WORKERS = 3
# MEASURED end to end on 3 real clips (299s of footage) running 3-up: 166s wall
# = 1.80x realtime AGGREGATE, errors none, output 1080x1920.
#
# Do NOT divide a per-clip figure by WORKERS to get this. An earlier version did
# exactly that and under-quoted the job by 6x. Parallelism barely helps here:
# one clip alone measured 1.86x, three together 1.80x, because every worker is
# pre-cutting ~280MB out of an 11.5GB source and they contend on disk long
# before the GPU is busy.
AGGREGATE_REALTIME = 1.80


def eta_minutes(clips: list[dict]) -> int:
    total = sum(float(c.get("duration_s") or 60) for c in clips)
    return max(1, round(total / AGGREGATE_REALTIME / 60)) if clips else 0


def one(job: dict, clip: dict, look: dict) -> dict:
    """Re-render a single clip with new look/sound. Returns the updated clip."""
    out = Path(clip["path"])
    srt = out.with_suffix(".srt")
    src = source_for(job)

    grade = color.fast_filter_chain(look["color_preset"], float(look["color_intensity"]))

    if src is not None:
        # Same path the original render took: lossless pre-cut, then one encode.
        seg = out.with_name(out.stem + "_relook_src.mp4")
        try:
            render.cut_segment(src, seg, float(clip["start_s"]), float(clip["duration_s"]))
            work, w_start = seg, 1.0
        except Exception:  # noqa: BLE001 - fall back to seeking the original
            seg = None
            work, w_start = src, float(clip["start_s"])
    else:
        # Degraded path: the finished clip is all we have. It already carries the
        # previous grade, so a second grade compounds — accepted knowingly.
        seg = None
        work, w_start = out, 0.0

    dur = float(clip["duration_s"])
    afilter, ainfo = audio_fx.build_chain(
        work, w_start, dur,
        preset=look["audio_preset"], intensity=float(look["audio_intensity"]))

    tmp = out.with_name(out.stem + "_relook.mp4")
    try:
        render.render_wide(work, tmp, w_start, w_start + dur,
                           afilter=afilter,
                           subs_path=srt if srt.exists() else None,
                           grade=grade)
        if clip.get("outro") and look.get("outro", True):
            geo = outro.probe(tmp)
            baked = outro.bake(geo["w"], geo["h"], geo["fps"], grade=grade,
                               afilter=afilter,
                               codec_args=render._video_codec_args(),
                               audio_args=render._audio_args())
            if baked:
                outro.append(tmp, baked)
        out.unlink(missing_ok=True)
        tmp.replace(out)
        clip["grade"] = color.describe(look["color_preset"], float(look["color_intensity"]))
        clip["audio_fx"] = audio_fx.describe(ainfo)
        clip["relooked_at"] = time.time()
        clip.pop("relook_error", None)
        # A saved crop was made from the old pixels.
        clip.pop("path_reframed", None)
        clip.pop("reframe", None)
    except Exception as exc:  # noqa: BLE001 - one bad clip must not sink the batch
        tmp.unlink(missing_ok=True)
        clip["relook_error"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if seg is not None:
            Path(seg).unlink(missing_ok=True)
    return clip


# --------------------------------------------------------------- batch runner
# Applying to a whole job is tens of minutes. It runs on its own thread and
# writes progress into the job record so the page can show it, and so closing
# the tab doesn't cancel it.
_RUNNING: dict[str, dict] = {}


def status(job_id: str) -> Optional[dict]:
    return _RUNNING.get(job_id)


def run_all(job_id: str, look: dict, load: Callable, update: Callable,
            scope: str = "all") -> dict:
    if job_id in _RUNNING and not _RUNNING[job_id].get("done"):
        return _RUNNING[job_id]

    job = load(job_id)
    clips = select(job or {}, scope)
    state = {"job_id": job_id, "total": len(clips), "done_count": 0,
             "done": False, "cancelled": False, "started_at": time.time(),
             "errors": 0, "current": ""}
    _RUNNING[job_id] = state

    lock = threading.Lock()

    def _one_clip(cid: str) -> None:
        if state["cancelled"]:
            return
        # Re-read under the lock: every worker mutates the same clips list, and
        # a stale read would resurrect a clip another worker already updated.
        with lock:
            fresh = load(job_id) or job
            target = next((x for x in fresh.get("clips", [])
                           if x.get("id") == cid), None)
            if target is None:
                return
            state["current"] = target.get("hook") or cid
            work_copy = dict(target)

        one(fresh, work_copy, look)

        with lock:
            latest = load(job_id) or fresh
            for i, x in enumerate(latest.get("clips", [])):
                if x.get("id") == cid:
                    latest["clips"][i] = work_copy
                    break
            if work_copy.get("relook_error"):
                state["errors"] += 1
            state["done_count"] += 1
            # Persist after every clip so progress survives a crash and the
            # review page shows clips updating as they land.
            update(job_id, clips=latest["clips"])

    def _work() -> None:
        # Three at a time, same as the original render. Serial was 1.9x realtime
        # per clip, which turned a 60-clip job into a four-hour wait.
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                list(pool.map(_one_clip, [c.get("id") for c in clips]))
        finally:
            state["done"] = True
            state["current"] = ""

    threading.Thread(target=_work, name=f"relook-{job_id}", daemon=True).start()
    return state


def cancel(job_id: str) -> bool:
    st = _RUNNING.get(job_id)
    if not st or st.get("done"):
        return False
    st["cancelled"] = True
    return True
