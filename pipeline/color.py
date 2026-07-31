"""Colour grading — a light, reversible version of what a colourist does.

Phone footage indoors is flat on purpose: the camera protects highlights and
keeps everything mid-grey so nothing clips. It's safe and it's lifeless. A grade
puts the contrast and colour back.

What a real grade does, in order, and what each maps to in ffmpeg:

  1. Lift/crush    set the black point so shadows are actually dark
                   -> curves, and eq brightness
  2. Contrast      widen the gap between dark and light
                   -> eq contrast
  3. White balance correct the room's colour cast
                   -> colorbalance (shadows/mids/highlights, per channel)
  4. Saturation    bring colour back without going cartoon
                   -> eq saturation
  5. Skin priority keep faces natural while the rest gets punchier
                   -> gentler saturation than contrast, warm mids
  6. Micro-sharpen phone video is soft after re-encode
                   -> unsharp, very light

Presets are per environment because the fix differs:
  * OFFICE  tungsten/LED indoor. Warm-yellow cast, flat, grey-ish. Cool the
            highlights, add contrast, moderate saturation.
  * GYM     fluorescent/mixed. Green cast is the classic problem. Pull green out
            of the mids, more contrast (gyms are usually dim and grey).
  * OUTDOOR already contrasty. Light touch, mostly saturation and a small lift.
  * NEUTRAL cast-free source. Contrast and saturation only.

EVERY preset scales with `intensity` 0..1, so a wrong guess is a dial, not a
ruined clip. intensity=0 returns no filter at all.
"""
from __future__ import annotations

PRESETS = {
    # `curve` is the S-curve strength (see _s_curve). It does most of the work:
    # raw `contrast` scales everything around mid-grey and clips, an S-curve
    # steepens the midtones where the subject lives while easing off at both
    # ends, so shadows go rich instead of black and skin doesn't blow out.
    # These are tuned for a phone feed, where flat loses to punchy every time.
    "office": {
        "label": "Office / indoor tungsten",
        "curve": 0.055, "contrast": 0.12, "saturation": 0.24,
        "gamma": -0.03, "brightness": -0.010,
        # A tungsten/LED room is heavily warm. Correct the cast across the WHOLE
        # tonal range before saturating — boosting saturation on an uncorrected
        # warm image just makes a more colourful yellow room (measured: it went
        # orange). Red down, blue up, green barely touched so skin stays skin.
        "shadows": (-0.03, 0.0, 0.05), "mids": (-0.04, -0.01, 0.06), "highs": (-0.05, -0.01, 0.06),
        "sharpen": 0.45,
    },
    "gym": {
        "label": "Gym / fluorescent",
        "curve": 0.065, "contrast": 0.16, "saturation": 0.26,
        "gamma": -0.06, "brightness": 0.01,
        # fluorescent green cast is the classic gym problem: pull green from mids
        "shadows": (0.0, -0.02, 0.03), "mids": (0.04, -0.06, 0.02), "highs": (0.03, -0.03, 0.0),
        "sharpen": 0.50,
    },
    "outdoor": {
        "label": "Outdoor / daylight",
        "curve": 0.035, "contrast": 0.06, "saturation": 0.20,
        "gamma": -0.02, "brightness": -0.02,
        "shadows": (0.0, 0.0, 0.02), "mids": (0.0, 0.0, 0.0), "highs": (0.01, 0.0, -0.02),
        "sharpen": 0.30,
    },
    "neutral": {
        "label": "Neutral punch",
        "curve": 0.050, "contrast": 0.12, "saturation": 0.22,
        "gamma": -0.03, "brightness": 0.0,
        "shadows": (0.0, 0.0, 0.0), "mids": (0.0, 0.0, 0.0), "highs": (0.0, 0.0, 0.0),
        "sharpen": 0.40,
    },
}


def _s_curve(strength: float) -> str:
    """A filmic S: pull the quarter-tone down, push the three-quarter-tone up.

    Endpoints stay pinned at 0 and 1 so nothing clips, and 0.5 stays put so the
    overall exposure doesn't drift — only the slope through the midtones changes.
    """
    # Clamped hard. MEASURED on real footage: a delta of 0.16 here turned an
    # office into a sunset — crushed shadows, blown highlights, orange skin.
    # Anything past ~0.08 stops reading as "graded" and starts reading as "broken".
    s = max(0.0, min(0.08, strength))
    if s < 0.005:
        return ""
    lo = 0.25 - s          # quarter-tone drops -> richer shadows
    hi = 0.75 + s          # three-quarter rises -> brighter highlights
    return f"curves=all='0/0 0.25/{lo:.3f} 0.5/0.5 0.75/{hi:.3f} 1/1'"

DEFAULT_PRESET = "office"


def filter_chain(preset: str = DEFAULT_PRESET, intensity: float = 1.0) -> str:
    """-> an ffmpeg video filter string, or "" when there's nothing to do.

    intensity scales every adjustment, so 0.5 is exactly half the look and 0 is
    the untouched clip.
    """
    try:
        k = float(intensity)
    except (TypeError, ValueError):
        k = 1.0
    k = max(0.0, min(1.5, k))
    if k <= 0.001:
        return ""

    p = PRESETS.get(str(preset or "").lower(), PRESETS[DEFAULT_PRESET])
    parts: list[str] = []

    # 1. the S-curve first, while the image is still linear-ish in its own terms.
    curve = _s_curve(p.get("curve", 0.0) * k)
    if curve:
        parts.append(curve)

    # 2-4. contrast / brightness / gamma / saturation.
    # eq contrast is 1.0-centred, saturation 1.0-centred, gamma 1.0-centred.
    contrast = 1.0 + p["contrast"] * k
    saturation = 1.0 + p["saturation"] * k
    gamma = 1.0 + p["gamma"] * k
    brightness = p["brightness"] * k
    parts.append(
        f"eq=contrast={contrast:.3f}:saturation={saturation:.3f}"
        f":gamma={gamma:.3f}:brightness={brightness:.3f}")

    # 3. white balance per tonal range
    sh, md, hi = p["shadows"], p["mids"], p["highs"]
    if any(abs(v) > 0.001 for v in (*sh, *md, *hi)):
        parts.append(
            "colorbalance="
            f"rs={sh[0] * k:.3f}:gs={sh[1] * k:.3f}:bs={sh[2] * k:.3f}:"
            f"rm={md[0] * k:.3f}:gm={md[1] * k:.3f}:bm={md[2] * k:.3f}:"
            f"rh={hi[0] * k:.3f}:gh={hi[1] * k:.3f}:bh={hi[2] * k:.3f}")

    # 6. micro-sharpen last, gently. Too much looks crunchy on skin.
    amount = p["sharpen"] * k
    if amount > 0.02:
        parts.append(f"unsharp=5:5:{amount:.2f}:5:5:0.0")

    return ",".join(parts)


def describe(preset: str, intensity: float) -> str:
    p = PRESETS.get(str(preset or "").lower())
    if not p or intensity <= 0.001:
        return "no grade"
    return f"{p['label']} @ {int(float(intensity) * 100)}%"
