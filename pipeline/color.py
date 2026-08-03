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

from pathlib import Path
from typing import Optional

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

# "auto" is not a look, it is a measurement. See pipeline/autotune.py: it reads
# the actual frames and derives the correction, which is the only thing that can
# tell a warm office from a green gym without being told.
AUTO = "auto"
# What "auto" falls back to if measurement fails. Must be a real key in PRESETS —
# DEFAULT_PRESET is used as the dict fallback everywhere below.
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


# ---------------------------------------------------------------- 3D LUT path
#
# WHY: the filter chain above is correct but slow, and PROFILED, the cost is the
# colour maths itself, not the codec. On 20s of 1080x1920:
#
#     decode + encode, no filters ......  3.7s
#     + the grade chain ............... 16.6s     <- the grade costs 13s
#       of which colorbalance alone ....  5.5s
#       and unsharp ....................  0.1s    (free, keep it)
#
# The chain also costs far more than the sum of its parts, because eq works in
# YUV while curves and colorbalance work in RGB, so frames bounce between colour
# spaces repeatedly. Measured attempts to fix that directly all made it WORSE:
# -filter_threads 8 -> 25.2s, 16 -> 35.4s, forcing format=gbrp once -> 78.3s.
#
# A 3D LUT collapses every one of those transforms into a single interpolated
# lookup per pixel — one filter, one format conversion, and the maths happens
# here in numpy ONCE per preset instead of per pixel per frame. This is exactly
# what a colourist ships: a .cube file, not a stack of live filters.
LUT_SIZE = 33          # the standard .cube grid; 33^3 = 35937 entries
LUT_DIR = Path(__file__).resolve().parent.parent / "data" / "luts"


def _spline(xs, ys, t):
    """Natural cubic spline through the curve's control points, matching what
    ffmpeg's `curves` filter does between them."""
    import numpy as np  # noqa: PLC0415
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    n = len(xs)
    h = np.diff(xs)
    alpha = np.zeros(n)
    alpha[1:-1] = 3 * ((ys[2:] - ys[1:-1]) / h[1:] - (ys[1:-1] - ys[:-2]) / h[:-1])
    l = np.ones(n); mu = np.zeros(n); z = np.zeros(n)  # noqa: E741
    for i in range(1, n - 1):
        l[i] = 2 * (xs[i + 1] - xs[i - 1]) - h[i - 1] * mu[i - 1]
        mu[i] = h[i] / l[i]
        z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / l[i]
    b = np.zeros(n); c = np.zeros(n); d = np.zeros(n)
    for i in range(n - 2, -1, -1):
        c[i] = z[i] - mu[i] * c[i + 1]
        b[i] = (ys[i + 1] - ys[i]) / h[i] - h[i] * (c[i + 1] + 2 * c[i]) / 3
        d[i] = (c[i + 1] - c[i]) / (3 * h[i])
    idx = np.clip(np.searchsorted(xs, t) - 1, 0, n - 2)
    dx = t - xs[idx]
    return ys[idx] + b[idx] * dx + c[idx] * dx ** 2 + d[idx] * dx ** 3


def _bake(preset: str, k: float):
    """Apply the whole grade to an RGB grid, in the same order as the filters."""
    import numpy as np  # noqa: PLC0415
    p = PRESETS.get(str(preset or "").lower(), PRESETS[DEFAULT_PRESET])
    n = LUT_SIZE
    ramp = np.linspace(0.0, 1.0, n)
    # .cube ordering: red varies fastest, then green, then blue.
    b, g, r = np.meshgrid(ramp, ramp, ramp, indexing="ij")
    rgb = np.stack([r, g, b], axis=-1)

    # 1. S-curve
    s = max(0.0, min(0.08, p.get("curve", 0.0) * k))
    if s >= 0.005:
        xs = [0.0, 0.25, 0.5, 0.75, 1.0]
        ys = [0.0, 0.25 - s, 0.5, 0.75 + s, 1.0]
        rgb = _spline(xs, ys, np.clip(rgb, 0, 1))

    # 2. gamma, contrast, brightness
    gamma = 1.0 + p["gamma"] * k
    rgb = np.clip(rgb, 1e-6, 1.0) ** (1.0 / max(gamma, 1e-3))
    rgb = (rgb - 0.5) * (1.0 + p["contrast"] * k) + 0.5 + p["brightness"] * k

    # 3. white balance, weighted by tonal range. The weights are the same
    #    shadow/mid/highlight falloff colorbalance uses.
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
    w_sh = np.clip(1.0 - lum * 2.0, 0, 1)[..., None]
    w_hi = np.clip(lum * 2.0 - 1.0, 0, 1)[..., None]
    w_md = np.clip(1.0 - np.abs(lum - 0.5) * 2.0, 0, 1)[..., None]
    shift = (np.array(p["shadows"]) * w_sh
             + np.array(p["mids"]) * w_md
             + np.array(p["highs"]) * w_hi) * k
    # colorbalance pushes toward the endpoint, so the effect fades where the
    # channel is already saturated — that's what keeps it from clipping.
    rgb = rgb + shift * (1.0 - np.abs(rgb - 0.5) * 2.0 * 0.5)

    # 4. saturation, around luma
    lum = (rgb @ np.array([0.2126, 0.7152, 0.0722]))[..., None]
    rgb = lum + (rgb - lum) * (1.0 + p["saturation"] * k)

    return np.clip(rgb, 0.0, 1.0)


def lut_path(preset: str = DEFAULT_PRESET, intensity: float = 1.0) -> Optional[Path]:
    """Write (and cache) a .cube for this preset/intensity. None = no grade."""
    try:
        k = max(0.0, min(1.5, float(intensity)))
    except (TypeError, ValueError):
        k = 1.0
    if k <= 0.001:
        return None
    key = f"{str(preset or DEFAULT_PRESET).lower()}_{k:.3f}_{LUT_SIZE}.cube"
    dest = LUT_DIR / key
    if dest.exists():
        return dest
    LUT_DIR.mkdir(parents=True, exist_ok=True)
    grid = _bake(preset, k).reshape(-1, 3)
    lines = [f"# Content Machine — {preset} @ {int(k * 100)}%",
             f"LUT_3D_SIZE {LUT_SIZE}", "DOMAIN_MIN 0.0 0.0 0.0",
             "DOMAIN_MAX 1.0 1.0 1.0", ""]
    lines += [f"{a:.6f} {b:.6f} {c:.6f}" for a, b, c in grid]
    tmp = dest.with_suffix(".cube.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(dest)      # write-then-rename: a reader never sees a half file
    return dest


# Hand-writing the LUT maths was a mistake worth recording: my colorbalance
# approximation drifted +7.6 on red versus the real filters, which put the warm
# cast back into the office preset — the exact thing that preset exists to fix.
#
# Instead, let ffmpeg bake the LUT with its OWN filters. A Hald CLUT is an image
# containing every colour; run the real chain over it once, and the result IS the
# lookup table. Fidelity is exact by construction, because the same code produced
# it. This is the standard way LUTs are captured from a live grade.
HALD_LEVEL = 8         # 8 -> 512x512 image, 64 samples/axis (262144 colours)


def hald_path(preset: str = DEFAULT_PRESET, intensity: float = 1.0) -> Optional[Path]:
    try:
        k = max(0.0, min(1.5, float(intensity)))
    except (TypeError, ValueError):
        k = 1.0
    if k <= 0.001:
        return None
    p = PRESETS.get(str(preset or "").lower(), PRESETS[DEFAULT_PRESET])
    # The sharpen is spatial — it cannot live in a colour lookup, so it stays a
    # live filter and is deliberately excluded from the bake.
    chain = ",".join(x for x in filter_chain(preset, k).split(",")
                     if not x.startswith("unsharp"))
    if not chain:
        return None

    dest = LUT_DIR / f"{str(preset).lower()}_{k:.3f}_hald{HALD_LEVEL}.png"
    if dest.exists():
        return dest
    LUT_DIR.mkdir(parents=True, exist_ok=True)

    import shutil as _sh  # noqa: PLC0415
    import subprocess as _sp  # noqa: PLC0415
    tmp = dest.with_suffix(".tmp.png")
    args = [_sh.which("ffmpeg") or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"haldclutsrc=level={HALD_LEVEL}",
            "-vf", chain, "-frames:v", "1", "-pix_fmt", "rgb24", str(tmp)]
    r = _sp.run(args, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)   # write-then-rename: a reader never sees a half file
    return dest


def fast_filter_chain(preset: str = DEFAULT_PRESET, intensity: float = 1.0) -> str:
    """Identical look to filter_chain, as one lookup plus the spatial sharpen.

    Falls back to the live chain if the bake fails, so a missing LUT costs speed
    and never correctness.
    """
    try:
        k = max(0.0, min(1.5, float(intensity)))
    except (TypeError, ValueError):
        k = 1.0
    if k <= 0.001:
        return ""
    hald = hald_path(preset, k)
    if hald is None:
        return filter_chain(preset, k)

    cube = _hald_to_cube(hald, preset, k)
    if cube is None:
        return filter_chain(preset, k)

    p = PRESETS.get(str(preset or "").lower(), PRESETS[DEFAULT_PRESET])
    amount = p["sharpen"] * k
    sharpen = f",unsharp=5:5:{amount:.2f}:5:5:0.0" if amount > 0.02 else ""
    esc = str(cube).replace("\\", "/").replace(":", r"\:")
    return f"lut3d=file='{esc}':interp=trilinear{sharpen}"


def _hald_to_cube(hald: Path, preset: str, k: float) -> Optional[Path]:
    """Convert the baked Hald image into a .cube.

    haldclut needs its CLUT as a second filter input, which does not compose with
    a plain -vf chain. lut3d takes a file, so it drops in anywhere. The VALUES are
    still ffmpeg's own, so fidelity is unchanged — only the container differs.
    """
    dest = hald.with_suffix(".cube")
    if dest.exists():
        return dest

    import shutil as _sh  # noqa: PLC0415
    import subprocess as _sp  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    side = HALD_LEVEL ** 2                    # colours per axis
    px = side ** 3                            # total entries
    img_w = img_h = int(round(px ** 0.5))
    r = _sp.run([_sh.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-i", str(hald), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                capture_output=True, timeout=300)
    if r.returncode != 0 or len(r.stdout) != img_w * img_h * 3:
        return None

    # Hald layout is exactly .cube order: red fastest, then green, then blue.
    data = np.frombuffer(r.stdout, dtype=np.uint8).reshape(-1, 3) / 255.0
    lines = [f"# Content Machine — {preset} @ {int(k * 100)}% (baked by ffmpeg)",
             f"LUT_3D_SIZE {side}", "DOMAIN_MIN 0.0 0.0 0.0",
             "DOMAIN_MAX 1.0 1.0 1.0", ""]
    lines.extend(f"{a:.5f} {b:.5f} {c:.5f}" for a, b, c in data)
    tmp = dest.with_suffix(".cube.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(dest)
    return dest


def describe(preset: str, intensity: float) -> str:
    if str(preset or "").lower() == AUTO:
        return "Auto — measured from the footage"
    p = PRESETS.get(str(preset or "").lower())
    if not p or intensity <= 0.001:
        return "no grade"
    return f"{p['label']} @ {int(float(intensity) * 100)}%"
