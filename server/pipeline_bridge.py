"""Seam between the job queue and the processing pipeline.

Kept deliberately thin and import-safe: if a heavy dependency (faster-whisper,
opencv) isn't installed yet, the server still boots and the job reports exactly
what's missing instead of the queue dying on an ImportError.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from . import config


def ffprobe(path: Path) -> dict:
    exe = shutil.which("ffprobe") or "ffprobe"
    cmd = [exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "{}")


def probe_summary(path: Path) -> dict:
    meta = ffprobe(path)
    fmt = meta.get("format", {})
    video = next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in meta.get("streams", []) if s.get("codec_type") == "audio"), {})
    try:
        duration = float(fmt.get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "duration_s": round(duration, 2),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "vcodec": video.get("codec_name"),
        "acodec": audio.get("codec_name"),
        "has_audio": bool(audio),
        "fps": video.get("r_frame_rate"),
        "bitrate": fmt.get("bit_rate"),
    }


def run(job: dict, report: Callable[..., None]) -> None:
    src = Path(job["source_path"])
    if not src.exists():
        raise FileNotFoundError(f"source video is gone: {src}")

    report(stage="probe", progress=0.03, message="Reading the file")
    info = probe_summary(src)
    if not info["has_audio"]:
        raise RuntimeError("this video has no audio track, so there's nothing to transcribe")
    report(stage="probe", progress=0.06,
           message=f"{info['width']}x{info['height']}, {info['duration_s'] / 60:.1f} min",
           duration_s=info["duration_s"], probe=info)

    try:
        from pipeline import run_pipeline  # noqa: PLC0415 - optional until Stage 1 lands
    except ImportError as exc:
        # Deliberately loud. The upload is safe on disk and re-runnable, but the
        # job did NOT produce clips, and reporting "done / ready for review" here
        # would be a lie the review page can't back up.
        raise RuntimeError(
            "Upload landed and probed OK, but the clipper isn't built yet "
            f"(Stage 1 pending: {exc}). Your file is saved at "
            f"{src.name} and will be re-run once Stage 1 lands."
        ) from exc

    run_pipeline(job=job, info=info, report=report, cfg=config)
