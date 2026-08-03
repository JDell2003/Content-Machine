"""Measure the footage, then decide the grade. No presets, no guessing.

A preset is a guess about a room you have not looked at. It is right until you
move, and then it is confidently wrong — an office grade dropped on a gym adds
warmth to a scene that is already green, and nobody notices until the clips are
rendered.

This measures the ACTUAL frames and derives the correction from what it finds:

    black point   1st percentile luma   -> lift or crush
    white point   99th percentile luma  -> pull down blown highlights
    exposure      mean luma             -> gamma
    contrast      luma std deviation    -> contrast
    colour cast   channel means IN THE HIGHLIGHTS

The cast is measured in the highlights on purpose. Walls, paper and ceilings are
meant to be neutral, so whatever tint they carry IS the room's cast. Averaging
the whole frame instead would read skin and wood as "too warm" and drain the
colour out of the people — the classic grey-world mistake.

It also refuses to over-correct. Skin needs to stay warm, so the red pull is
capped and the neutral target keeps a little warmth rather than aiming at pure
grey.

CLOSED LOOP: apply, re-measure, adjust, up to `passes` times. It stops as soon
as everything is inside tolerance, so an already-good clip costs one measurement
and no correction at all.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

# What "looks right" means, in numbers, on a 0-255 scale.
TARGET = {
    "black": (0.0, 12.0),      # 1st percentile — crushed but not clipped
    "white": (232.0, 250.0),   # 99th percentile — bright but not blown
    "mean": (98.0, 128.0),     # overall exposure
    "contrast": (46.0, 66.0),  # luma std dev
    # How neutral the highlights should be. Not 0: a room with SOME warmth reads
    # as inviting, a perfectly neutral one reads as a hospital.
    "cast": 8.0,               # max acceptable R-B spread in the highlights
}
WARMTH_KEEP = 6.0              # deliberately leave this much R-over-B


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def sample(src: Path, at: float = 0.0, vf: str = "", n: int = 3,
           spread: float = 20.0) -> Optional[dict]:
    """Measure N frames and average. One frame can be a blink or a flash."""
    import numpy as np  # noqa: PLC0415

    rows = []
    for i in range(max(1, n)):
        t = max(0.0, at + i * spread)
        chain = f"{vf},scale=160:-2" if vf else "scale=160:-2"
        p = subprocess.run(
            [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}",
             "-i", str(src), "-frames:v", "1", "-vf", chain,
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=180)
        if p.returncode != 0 or len(p.stdout) < 300:
            continue
        rows.append(np.frombuffer(p.stdout, dtype=np.uint8).astype(float).reshape(-1, 3))
    if not rows:
        return None

    px = np.concatenate(rows, axis=0)
    lum = px @ np.array([0.2126, 0.7152, 0.0722])
    # Highlights only: this is where a neutral surface should be neutral.
    hi = px[lum >= np.percentile(lum, 80)]
    hi_mean = hi.mean(axis=0) if len(hi) else px.mean(axis=0)
    return {
        "black": float(np.percentile(lum, 1)),
        "white": float(np.percentile(lum, 99)),
        "mean": float(lum.mean()),
        "contrast": float(lum.std()),
        "rgb": [float(x) for x in px.mean(axis=0)],
        "hi_rgb": [float(x) for x in hi_mean],
        "cast": float(hi_mean[0] - hi_mean[2]),   # + = warm, - = cool
    }


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def derive(m: dict, prev: Optional[dict] = None) -> dict:
    """Turn a measurement into grade parameters."""
    p = dict(prev or {"contrast": 1.0, "gamma": 1.0, "brightness": 0.0,
                      "saturation": 1.0, "r": 0.0, "b": 0.0, "black_in": 0.0,
                      "white_in": 255.0})

    # Tonal range: map the measured black/white points onto the targets. This is
    # a levels adjustment, which fixes washed-out footage far more directly than
    # winding contrast up (that just clips both ends).
    if m["black"] > TARGET["black"][1]:
        p["black_in"] = min(m["black"] * 0.85, 40.0)
    if m["white"] < TARGET["white"][0]:
        p["white_in"] = max(m["white"] * 1.02, 180.0)
    elif m["white"] > TARGET["white"][1] + 4:
        # Blown: pull the top down rather than clip it.
        p["white_in"] = 255.0
        p["brightness"] = _clamp(p["brightness"] - 0.02, -0.12, 0.12)

    lo, hi = TARGET["mean"]
    if m["mean"] < lo:
        p["gamma"] = _clamp(p["gamma"] + (lo - m["mean"]) / 260.0, 0.75, 1.4)
        if p["gamma"] >= 1.39:      # gamma maxed, finish the job with brightness
            p["brightness"] = _clamp(p["brightness"] + (lo - m["mean"]) / 900.0, -0.18, 0.18)
    elif m["mean"] > hi:
        p["gamma"] = _clamp(p["gamma"] - (m["mean"] - hi) / 260.0, 0.75, 1.4)
        if p["gamma"] <= 0.76:      # gamma bottomed out, pull exposure instead
            p["brightness"] = _clamp(p["brightness"] - (m["mean"] - hi) / 900.0, -0.18, 0.18)

    clo, chi = TARGET["contrast"]
    if m["contrast"] < clo:
        p["contrast"] = _clamp(p["contrast"] + (clo - m["contrast"]) / 90.0, 0.9, 1.45)
    elif m["contrast"] > chi:
        p["contrast"] = _clamp(p["contrast"] - (m["contrast"] - chi) / 120.0, 0.9, 1.45)

    # Colour cast, measured in the highlights. Correct toward WARMTH_KEEP, not 0.
    excess = m["cast"] - WARMTH_KEEP
    if abs(excess) > TARGET["cast"] * 0.5:
        step = _clamp(excess / 255.0 * 0.9, -0.09, 0.09)
        p["r"] = _clamp(p["r"] - step, -0.14, 0.14)
        p["b"] = _clamp(p["b"] + step, -0.14, 0.14)

    # Saturation last, and gently. Correcting a cast already changes how
    # colourful it looks, so a big saturation move on top double-counts.
    sat_now = 0.0
    rgb = m["rgb"]
    if sum(rgb) > 0:
        sat_now = (max(rgb) - min(rgb)) / (sum(rgb) / 3)
    if sat_now < 0.22:
        p["saturation"] = _clamp(p["saturation"] + 0.06, 0.9, 1.35)
    elif sat_now > 0.42:
        p["saturation"] = _clamp(p["saturation"] - 0.06, 0.9, 1.35)
    return p


def to_filter(p: dict) -> str:
    """Grade parameters -> an ffmpeg filter chain."""
    parts = []
    if p.get("black_in", 0) > 0.5 or p.get("white_in", 255) < 254:
        b = p.get("black_in", 0.0) / 255.0
        w = p.get("white_in", 255.0) / 255.0
        # A levels remap: this is what actually fixes washed-out footage.
        parts.append(f"curves=all='{b:.4f}/0 {w:.4f}/1'")
    parts.append(
        f"eq=contrast={p['contrast']:.3f}:gamma={p['gamma']:.3f}"
        f":brightness={p['brightness']:.3f}:saturation={p['saturation']:.3f}")
    if abs(p.get("r", 0)) > 0.004 or abs(p.get("b", 0)) > 0.004:
        parts.append(f"colorbalance=rm={p['r']:.3f}:bm={p['b']:.3f}"
                     f":rh={p['r'] * 0.8:.3f}:bh={p['b'] * 0.8:.3f}")
    return ",".join(parts)


def within_target(m: dict) -> bool:
    return (TARGET["black"][0] <= m["black"] <= TARGET["black"][1]
            and TARGET["white"][0] <= m["white"] <= TARGET["white"][1]
            and TARGET["mean"][0] <= m["mean"] <= TARGET["mean"][1]
            and TARGET["contrast"][0] <= m["contrast"] <= TARGET["contrast"][1]
            and abs(m["cast"] - WARMTH_KEEP) <= TARGET["cast"])


def auto_grade(src: Path, at: float = 0.0, passes: int = 4) -> dict:
    """Measure -> correct -> re-measure, until it lands or the passes run out.

    -> {filter, before, after, passes_used, converged}. `filter` is "" when the
    footage is already fine, which is a real outcome and not a failure.
    """
    before = sample(src, at)
    if not before:
        return {"filter": "", "before": None, "after": None,
                "passes_used": 0, "converged": False,
                "note": "could not read frames"}

    if within_target(before):
        return {"filter": "", "before": before, "after": before,
                "passes_used": 0, "converged": True,
                "note": "already within target — left alone"}

    p, m, used = None, before, 0
    for i in range(max(1, passes)):
        p = derive(m, p)
        vf = to_filter(p)
        used = i + 1
        m2 = sample(src, at, vf)
        if not m2:
            break
        m = m2
        if within_target(m):
            break

    return {"filter": to_filter(p) if p else "", "params": p,
            "before": before, "after": m, "passes_used": used,
            "converged": within_target(m),
            "note": "" if within_target(m) else "closest it got within the pass limit"}
