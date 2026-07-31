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


def plan(job: dict) -> dict:
    """Can we re-render losslessly, or only re-encode what's left?"""
    src = source_for(job)
    clips = [c for c in job.get("clips", []) if c.get("path")]
    return {
        "clips": len(clips),
        "lossless": src is not None,
        "source": str(src) if src else None,
        "note": ("Re-cuts from the original recording — same quality as the first render."
                 if src else
                 "The source recording has been swept (48h retention). Re-grading now "
                 "would re-encode the finished clips, costing one generation of quality."),
        # Measured ~45s/clip on NVENC at 4K.
        "eta_minutes": round(len(clips) * 45 / 60) if clips else 0,
    }


def one(job: dict, clip: dict, look: dict) -> dict:
    """Re-render a single clip with new look/sound. Returns the updated clip."""
    out = Path(clip["path"])
    srt = out.with_suffix(".srt")
    src = source_for(job)

    grade = color.filter_chain(look["color_preset"], float(look["color_intensity"]))

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


def run_all(job_id: str, look: dict, load: Callable, update: Callable) -> dict:
    if job_id in _RUNNING and not _RUNNING[job_id].get("done"):
        return _RUNNING[job_id]

    job = load(job_id)
    clips = [c for c in (job or {}).get("clips", []) if c.get("path")]
    state = {"job_id": job_id, "total": len(clips), "done_count": 0,
             "done": False, "cancelled": False, "started_at": time.time(),
             "errors": 0, "current": ""}
    _RUNNING[job_id] = state

    def _work() -> None:
        try:
            for c in clips:
                if state["cancelled"]:
                    break
                state["current"] = c.get("hook") or c.get("id") or ""
                fresh = load(job_id) or job
                target = next((x for x in fresh.get("clips", [])
                               if x.get("id") == c.get("id")), None)
                if target is None:
                    continue
                one(fresh, target, look)
                if target.get("relook_error"):
                    state["errors"] += 1
                state["done_count"] += 1
                # Persist after every clip so progress survives a crash and the
                # review page shows clips updating one at a time.
                update(job_id, clips=fresh["clips"])
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
