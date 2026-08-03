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
    # Widened after looking at the result: a frame measuring 136 was well
    # exposed and pleasant, and the tighter window kept pulling it darker for
    # no visible gain. Numbers serve the picture, not the other way round.
    "mean": (100.0, 140.0),    # overall exposure
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
        # Per-pass step is small so it converges rather than overshoots, but the
        # TOTAL has to be wide enough for a genuinely bad room. Measured: a warm
        # office reads +45, and a +/-0.14 ceiling saturated at +23 — visibly
        # orange still. Skin is protected by correcting red and blue only and
        # leaving green alone, so widening this does not turn faces grey.
        step = _clamp(excess / 255.0 * 1.1, -0.10, 0.10)
        p["r"] = _clamp(p["r"] - step, -0.26, 0.26)
        p["b"] = _clamp(p["b"] + step, -0.26, 0.26)

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


def auto_grade(src: Path, at: float = 0.0, passes: int = 5) -> dict:
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


# ============================================================== audio
# Same principle as the grade: measure, then decide. A voice preset is a guess
# about a room, and the wrong guess is audible — too much denoise on clean audio
# is the metallic warble, too little on a noisy room is a hiss under every word.
#
# What gets measured, and what each drives:
#
#     noise floor + peak  ->  how much denoise is justified
#     spectral tilt       ->  presence lift (dull) or de-ess (harsh)
#     low-frequency energy->  where the highpass sits
#     crest factor        ->  whether compression is warranted AT ALL
#     integrated loudness ->  the final gain
#
# The compressor is the important one. MEASURED on real footage: the fixed chain
# came out at 44.3 dB separation against 49.9 dB for the untouched audio — it was
# making things WORSE, because compand pulls quiet passages up and drags the room
# noise with them. So compression is now applied only when the delivery is
# genuinely uneven, and never by default.

A_TARGET = {
    "separation": 52.0,   # dB between peak and noise floor; above this is clean
    "lufs": -14.0,        # what short-form platforms normalise to
    "tilt": (0.28, 0.52),  # high-band / low-band energy ratio; outside = dull or harsh
    "crest": 14.0,        # dB peak-to-RMS; above this the delivery is uneven
}


def _astats(src: Path, af: str = "", start: float = 0.0,
            dur: float = 30.0) -> Optional[dict]:
    """One ffmpeg pass for level statistics."""
    import re  # noqa: PLC0415
    chain = (af + "," if af else "") + "astats=measure_perchannel=none"
    p = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{start:.2f}",
         "-i", str(src), "-t", f"{dur:.2f}", "-map", "0:a:0",
         "-af", chain, "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600)
    txt = p.stderr or ""

    def grab(label: str) -> Optional[float]:
        m = re.findall(rf"{label}:\s+(-?[\d.]+)", txt)
        return float(m[-1]) if m else None

    floor, peak, rms = grab("Noise floor dB"), grab("Peak level dB"), grab("RMS level dB")
    if floor is None or peak is None:
        return None
    return {"floor": floor, "peak": peak, "rms": rms if rms is not None else peak,
            "separation": peak - floor, "crest": (peak - rms) if rms is not None else 0.0}


def _loudness(src: Path, af: str = "", start: float = 0.0,
              dur: float = 30.0) -> Optional[float]:
    import re  # noqa: PLC0415
    chain = (af + "," if af else "") + "ebur128"
    p = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{start:.2f}",
         "-i", str(src), "-t", f"{dur:.2f}", "-map", "0:a:0",
         "-af", chain, "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600)
    vals = re.findall(r"I:\s+(-?[\d.]+) LUFS", p.stderr or "")
    return float(vals[-1]) if vals else None


def _tilt(src: Path, start: float = 0.0, dur: float = 30.0) -> float:
    """High-band vs low-band energy. Low = dull/muffled, high = thin/harsh."""
    lo = _astats(src, "lowpass=f=1000", start, dur)
    hi = _astats(src, "highpass=f=3000", start, dur)
    if not lo or not hi:
        return 0.4
    # RMS is in dB; convert both to linear before taking the ratio.
    l_lin = 10 ** (lo["rms"] / 20)
    h_lin = 10 ** (hi["rms"] / 20)
    return h_lin / max(l_lin + h_lin, 1e-9)


def measure_voice(src: Path, start: float = 0.0, dur: float = 30.0) -> Optional[dict]:
    base = _astats(src, "", start, dur)
    if not base:
        return None
    return {**base, "lufs": _loudness(src, "", start, dur) or -20.0,
            "tilt": _tilt(src, start, dur)}


def derive_voice(m: dict, model_path: Optional[Path] = None) -> dict:
    """Measurement -> voice chain parameters."""
    p: dict = {}

    # Denoise only as much as the noise actually justifies. A clean recording
    # gets none, which is what stops the metallic artifact appearing on audio
    # that never needed treating.
    short = A_TARGET["separation"] - m["separation"]
    p["denoise_wet"] = _clamp(short / 22.0, 0.0, 0.85) if short > 2 else 0.0

    # Highpass: rumble lives under ~90 Hz and never carries speech.
    p["hpf"] = 85.0

    # Presence follows the measured tilt rather than a fixed +2 dB.
    t = m["tilt"]
    if t < A_TARGET["tilt"][0]:
        p["presence"] = _clamp((A_TARGET["tilt"][0] - t) * 22.0, 0.0, 4.0)
        p["deess"] = 0.0
    elif t > A_TARGET["tilt"][1]:
        p["presence"] = 0.0
        p["deess"] = _clamp((t - A_TARGET["tilt"][1]) * 2.0, 0.0, 0.6)
    else:
        p["presence"], p["deess"] = 1.0, 0.25

    # Boxiness sits around 300 Hz in small rooms; scale it with how much
    # low-mid energy there is rather than always cutting 3 dB.
    p["warmth"] = -3.0 if t < 0.45 else -1.5

    # COMPRESSION ONLY IF THE DELIVERY IS UNEVEN. This is the fix for the chain
    # measuring worse than untouched audio.
    p["compress"] = m["crest"] > A_TARGET["crest"]
    p["gain_db"] = 0.0
    return p


def voice_filter(p: dict, model: Optional[Path] = None) -> str:
    parts = [f"highpass=f={int(p.get('hpf', 85))}", "lowpass=f=14000"]
    wet = float(p.get("denoise_wet", 0.0))
    if wet > 0.02 and model and model.exists():
        esc = str(model).replace("\\", "/").replace(":", r"\:")
        dry = 1.0 - wet
        parts.append(
            f"asplit=2[cd][cw];[cw]arnndn=m='{esc}'[cn];"
            f"[cd][cn]amix=inputs=2:weights={dry:.3f} {wet:.3f}:normalize=0")
    if abs(p.get("warmth", 0)) > 0.1:
        parts.append(f"equalizer=f=300:t=q:w=1.2:g={p['warmth']:.1f}")
    if p.get("presence", 0) > 0.1:
        parts.append(f"equalizer=f=3000:t=q:w=1.5:g={p['presence']:.1f}")
    if p.get("deess", 0) > 0.05:
        parts.append(f"deesser=i={p['deess']:.2f}")
    if p.get("compress"):
        parts.append("compand=attacks=0.02:decays=0.3:"
                     "points=-70/-80|-45/-40|-20/-14|0/-8")
    return ",".join(parts)


def auto_voice(src: Path, start: float = 0.0, dur: float = 30.0,
               model: Optional[Path] = None) -> dict:
    """Measure -> derive -> verify.

    The verify step is the point. If the shaping made the separation WORSE than
    the untouched audio, the treatment is hurting rather than helping, and it
    backs off instead of shipping it. That is exactly the failure the fixed
    chain had: 44.3 dB against 49.9 dB for doing nothing.
    """
    from . import audio_fx  # noqa: PLC0415
    if model is None:
        model = audio_fx.model_path()

    before = measure_voice(src, start, dur)
    if not before:
        return {"filter": "", "before": None, "after": None,
                "summary": "could not read the audio"}

    p = derive_voice(before)
    shaped = voice_filter(p, model)
    after = _astats(src, shaped, start, dur)

    if after and after["separation"] < before["separation"] - 1.0:
        # Treatment is losing to silence. Drop the compressor first (it is the
        # usual culprit) and cap the denoise, then re-check.
        p["compress"] = False
        p["denoise_wet"] = min(p["denoise_wet"], 0.35)
        shaped = voice_filter(p, model)
        after = _astats(src, shaped, start, dur)

    meas = audio_fx.measure_loudness(src, start, dur, shaped)
    norm = f"loudnorm=I={A_TARGET['lufs']}:TP=-1.5:LRA=11"
    if meas:
        try:
            norm = (f"loudnorm=I={A_TARGET['lufs']}:TP=-1.5:LRA=11"
                    f":measured_I={float(meas['input_i'])}"
                    f":measured_TP={float(meas['input_tp'])}"
                    f":measured_LRA={float(meas['input_lra'])}"
                    f":measured_thresh={float(meas['input_thresh'])}"
                    f":offset={float(meas.get('target_offset', 0.0))}:linear=true")
        except (KeyError, TypeError, ValueError):
            pass

    bits = [f"denoise {int(p['denoise_wet'] * 100)}%" if p["denoise_wet"] > 0.02
            else "no denoise needed",
            "compressed" if p["compress"] else "no compression",
            f"presence +{p.get('presence', 0):.1f}dB",
            f"{before['lufs']:.0f} -> {A_TARGET['lufs']:.0f} LUFS"]
    return {"filter": f"{shaped},{norm}", "params": p,
            "before": before, "after": after, "summary": ", ".join(bits)}
