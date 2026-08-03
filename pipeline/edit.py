"""One edit spec -> ONE render.

Why this module exists: captions, colour/voice, and aspect each already had a
working endpoint, but each one re-rendered the clip on its own. Changing all
three cost three encodes and three waits. An editor where every tweak triggers
its own render is not an editor, it's a queue.

So the UI collects everything into a single spec and this applies the lot in one
pass from the ORIGINAL source. That is one generation of compression no matter
how many things you changed — the same rule the whole renderer is built on.

THE SPEC (all times are seconds RELATIVE to the start of the clip):

    {
      "trim":     [2.5, 48.0],          keep only this span
      "cuts":     [[12.0, 15.5]],       remove these spans from inside the trim
      "captions": {"cues": [...], "size": 20, "margin": 70,
                   "color": "#FFFFFF", "outline_color": "#000000", "outline": 4},
      "audio":    {"preset": "office", "intensity": 1.0, "volume_db": 0},
      "color":    {"preset": "office", "intensity": 1.0},
      "aspect":   {"ratio": "9:16", "pan": 0.62, "zoom": 1.0}
    }

Every key is optional; anything absent keeps what the clip already had.

TRIM AND CUTS BECOME "KEEPS"
The renderer already knows how to drop spans in-pass (select/aselect + setpts).
This converts trim + cuts into the keep-ranges it wants, so cutting costs
nothing extra — it happens inside the same encode as everything else.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from . import audio_fx, captions, color, reframe, render


def keeps_from(spec: dict, duration: float) -> list[tuple[float, float]]:
    """trim + cuts -> the spans to KEEP, ordered and non-overlapping."""
    trim = spec.get("trim") or [0.0, duration]
    try:
        t0 = max(0.0, float(trim[0]))
        t1 = min(float(duration), float(trim[1]))
    except (TypeError, ValueError, IndexError):
        t0, t1 = 0.0, float(duration)
    if t1 - t0 < 0.2:
        t0, t1 = 0.0, float(duration)

    cuts = []
    for c in spec.get("cuts") or []:
        try:
            a, b = max(t0, float(c[0])), min(t1, float(c[1]))
            if b - a > 0.05:
                cuts.append((a, b))
        except (TypeError, ValueError, IndexError):
            continue
    cuts.sort()

    keeps: list[tuple[float, float]] = []
    cursor = t0
    for a, b in cuts:
        if a > cursor:
            keeps.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < t1:
        keeps.append((cursor, t1))
    # No keeps means the cuts ate everything — treat that as "no cuts" rather
    # than rendering a zero-length file.
    return keeps or [(t0, t1)]


def crop_filter(spec: dict, src: Path) -> tuple[str, str]:
    """-> (crop filter, delivery scale). Empty strings mean 'leave it alone'."""
    asp = spec.get("aspect") or {}
    ratio = str(asp.get("ratio") or "").strip()
    if not ratio or ratio == "original" or ratio not in reframe.ASPECTS:
        return "", ""
    try:
        # display_dims, NOT probe: the filter chain sees the auto-rotated frame,
        # and an iPhone portrait clip is stored landscape. Using stored dims here
        # produced a crop box for the wrong orientation.
        w, h = reframe.display_dims(src)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Do not swallow this silently. A caught error here is exactly how
        # "choose 4:5, nothing happens, no message" hid for a whole build.
        raise RuntimeError(f"could not read dimensions for the crop: {exc}") from exc
    box = reframe.plan(w, h, ratio,
                       zoom=float(asp.get("zoom") or 1.0),
                       cx=float(asp.get("cx") or 0.5),
                       cy=float(asp.get("pan") or 0.5))
    if not box:
        return "", ""
    target = reframe.DELIVERY.get(ratio)
    scale = (f"scale={target[0]}:{target[1]}:flags=lanczos" if target else "")
    return f"crop={box['w']}:{box['h']}:{box['x']}:{box['y']}", scale


def apply(job: dict, clip: dict, spec: dict, *, source: Optional[Path] = None) -> dict:
    """Render the clip with every edit applied at once. Mutates and returns clip."""
    out = Path(clip["path"])
    srt = out.with_suffix(".srt")
    duration = float(clip.get("duration_s") or 0) or 1.0

    # Caption text edits are written to the .srt BEFORE rendering, so the burn-in
    # picks them up in the same pass.
    cap_spec = spec.get("captions") or {}
    if cap_spec.get("cues"):
        captions.write_srt(srt, cap_spec["cues"])
    style = {k: cap_spec[k] for k in
             ("size_wide", "margin_wide", "outline", "color", "outline_color",
              "x", "width")
             if k in cap_spec}
    # The UI sends friendlier names; map them.
    if "size" in cap_spec:
        style["size_wide"] = cap_spec["size"]
    if "margin" in cap_spec:
        style["margin_wide"] = cap_spec["margin"]

    # Work from the original recording when it's still on disk — that keeps this
    # at one encode from source rather than a second generation on the clip.
    src = source
    if src is None:
        raw = job.get("source_path") or job.get("path")
        src = Path(raw) if raw and Path(raw).exists() else None

    seg = None
    if src is not None:
        seg = out.with_name(out.stem + "_edit_src.mp4")
        try:
            render.cut_segment(src, seg, float(clip["start_s"]), duration)
            work, w_start = seg, 1.0
        except Exception:  # noqa: BLE001 - fall back to seeking the original
            seg = None
            work, w_start = src, float(clip["start_s"])
    else:
        work, w_start = out, 0.0     # degraded: source swept, costs a generation

    keeps = keeps_from(spec, duration)
    # keeps are relative to the clip; the renderer's select filter is relative to
    # the work file's timeline, which starts at w_start.
    keeps_abs = [(w_start + a, w_start + b) for a, b in keeps]
    kept = sum(b - a for a, b in keeps)

    # AUTO measures this clip's own segment and derives the chain. That is why
    # an already-rendered clip can still be fixed: the burn happens here, at
    # approve time, from the original source — not at batch-render time.
    from . import autotune as _auto  # noqa: PLC0415
    a_spec = spec.get("audio") or {}
    ainfo: dict = {}
    if str(a_spec.get("preset", "auto")).lower() == "auto":
        try:
            v = _auto.auto_voice(work, start=w_start, dur=min(30.0, duration))
            afilter = v.get("filter") or ""
            ainfo = {"auto": True, "summary": v.get("summary", "")}
        except Exception:  # noqa: BLE001 - never lose a clip over a voice chain
            afilter, ainfo = audio_fx.build_chain(work, w_start, duration)
    else:
        afilter, ainfo = audio_fx.build_chain(
            work, w_start, duration,
            preset=a_spec.get("preset", "office"),
            intensity=float(a_spec.get("intensity", 1.0)))
    vol = float(a_spec.get("volume_db") or 0)
    if abs(vol) > 0.1:
        afilter = f"{afilter},volume={vol:.1f}dB" if afilter else f"volume={vol:.1f}dB"

    c_spec = spec.get("color") or {}
    grade_note = ""
    if str(c_spec.get("preset", "auto")).lower() == "auto":
        try:
            g = _auto.auto_grade(work, at=w_start + min(5.0, duration / 3))
            grade = g.get("filter") or ""
            b, a2 = g.get("before"), g.get("after")
            if b and a2:
                grade_note = (f"cast {b['cast']:+.0f}->{a2['cast']:+.0f}, "
                              f"black {b['black']:.0f}->{a2['black']:.0f}")
        except Exception:  # noqa: BLE001
            grade = color.fast_filter_chain("office", 1.0)
    else:
        grade = color.fast_filter_chain(c_spec.get("preset", "office"),
                                        float(c_spec.get("intensity", 1.0)))
    crop, delivery = crop_filter(spec, work)

    from server import config as _cfg  # noqa: PLC0415
    will_append = bool(clip.get("outro"))
    fade_out = float(getattr(_cfg, "OUTRO_FADE_OUT", 0.5)) if will_append else 0.0

    tmp = out.with_name(out.stem + "_edit.mp4")
    try:
        render.render_wide(
            work, tmp, w_start, w_start + duration,
            keeps=keeps_abs if len(keeps) > 1 or kept < duration - 0.05 else None,
            afilter=afilter, subs_path=srt if srt.exists() else None,
            grade=grade, style_override=style or None,
            crop=crop, delivery=delivery,
            fade_out=fade_out, out_duration=kept)

        # The outro lives on the end of the old file and was just discarded.
        if clip.get("outro"):
            from . import outro as _o  # noqa: PLC0415
            geo = _o.probe(tmp)
            from server import config as _cfg  # noqa: PLC0415
            baked = _o.bake(geo["w"], geo["h"], geo["fps"], grade=grade,
                            afilter=afilter,
                            codec_args=render._video_codec_args(),
                            audio_args=render._audio_args(),
                            fade_in=float(getattr(_cfg, "OUTRO_FADE_IN", 0.5)))
            if baked:
                _o.append(tmp, baked)

        out.unlink(missing_ok=True)
        tmp.replace(out)
        clip["duration_s"] = round(kept, 2)
        clip["edited"] = True
        # Captions are burned HERE now, not during the batch render.
        clip["captions_burned"] = bool(srt.exists())
        clip["edit_spec"] = spec
        clip["grade"] = (f"Auto — {grade_note}" if grade_note
                         else color.describe(c_spec.get("preset", "office"),
                                             float(c_spec.get("intensity", 1.0))))
        clip["audio_fx"] = (f"Auto — {ainfo.get('summary', '')}" if ainfo.get("auto")
                            else audio_fx.describe(ainfo))
        clip.pop("edit_error", None)
        clip.pop("path_reframed", None)   # any saved crop was of the old pixels
        clip.pop("reframe", None)
    except Exception as exc:  # noqa: BLE001 - a failed edit must not destroy the clip
        tmp.unlink(missing_ok=True)
        clip["edit_error"] = f"{type(exc).__name__}: {exc}"[:250]
    finally:
        if seg is not None:
            Path(seg).unlink(missing_ok=True)
    return clip
