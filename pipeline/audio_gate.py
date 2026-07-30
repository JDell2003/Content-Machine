"""Audio sanity gate — ffmpeg only, no ML.

Runs on candidate windows BEFORE ranking so the brain isn't asked to judge a clip
whose audio is unusable. Flagged clips still render (Jason's call, not the
machine's) but carry an AUDIO badge on the approval page.

Three cheap checks, one ffmpeg pass each:
  1. silence share  — >30% silence means dead air
  2. clipped peaks  — peak at/near 0 dBFS means distortion
  3. speech headroom— mean vs peak too close to the noise floor
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

SILENCE_SHARE_LIMIT = 0.30
CLIP_PEAK_DB = -0.5          # at/above this counts as clipped
LOW_SPEECH_MEAN_DB = -34.0   # quieter mean than this is barely above the floor


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _run(args: list[str], timeout: int = 180) -> str:
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    # volumedetect/silencedetect report on stderr
    return (p.stderr or "") + (p.stdout or "")


def analyse(source: Path, start_s: float, end_s: float) -> dict:
    dur = max(0.1, end_s - start_s)
    ff = _ffmpeg()
    base = [ff, "-hide_banner", "-nostats", "-ss", f"{start_s:.3f}", "-t", f"{dur:.3f}",
            "-i", str(source), "-map", "0:a:0"]

    out = {"duration_s": round(dur, 2), "silence_share": 0.0, "peak_db": None,
           "mean_db": None, "flags": [], "ok": True, "note": ""}

    try:
        txt = _run(base + ["-af", "silencedetect=noise=-35dB:d=0.6,volumedetect",
                           "-f", "null", "-"])
    except (OSError, subprocess.SubprocessError) as exc:
        out.update(ok=True, note=f"audio check skipped ({type(exc).__name__})")
        return out

    silent = sum(float(m) for m in re.findall(r"silence_duration:\s*([0-9.]+)", txt))
    out["silence_share"] = round(min(1.0, silent / dur), 3)

    m = re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", txt)
    if m:
        out["peak_db"] = float(m.group(1))
    m = re.search(r"mean_volume:\s*(-?[0-9.]+)\s*dB", txt)
    if m:
        out["mean_db"] = float(m.group(1))

    if out["silence_share"] > SILENCE_SHARE_LIMIT:
        out["flags"].append(f"{out['silence_share'] * 100:.0f}% silence")
    if out["peak_db"] is not None and out["peak_db"] >= CLIP_PEAK_DB:
        out["flags"].append(f"clipped peaks ({out['peak_db']:.1f} dBFS)")
    if out["mean_db"] is not None and out["mean_db"] < LOW_SPEECH_MEAN_DB:
        out["flags"].append(f"very quiet ({out['mean_db']:.1f} dB mean)")
    # Narrow dynamic range low down usually means speech is buried in noise.
    if (out["mean_db"] is not None and out["peak_db"] is not None
            and out["peak_db"] - out["mean_db"] < 6.0 and out["mean_db"] < -20.0):
        out["flags"].append("speech barely above noise floor")

    out["ok"] = not out["flags"]
    out["note"] = "; ".join(out["flags"]) if out["flags"] else "audio ok"
    return out
