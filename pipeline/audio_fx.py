"""Audio cleanup: denoise, dereverb, shaping, loudness normalisation.

Three problems, all heard on the real footage:
  * room noise / HVAC / handling rumble riding under the speech
  * a whole clip sitting around -35 dB, inaudible next to every other post
  * room echo, because a hard-surfaced office reflects everything back

Everything is ffmpeg-native, so it folds into the SINGLE render pass and doesn't
break the "no intermediate re-encode" rule. The only external asset is the
RNNoise model used by ffmpeg's `arnndn` filter (github.com/richardpl/arnndn-models,
BSD) — a 290 KB weights file, not a runtime dependency.


THE METALLIC / WARBLING ARTIFACT
--------------------------------
RNNoise is a spectral masker: it decides, band by band and frame by frame, what
is speech and what is noise, then attenuates the rest. When it guesses wrong it
drops a band mid-syllable. Bands flickering in and out is exactly what "warbly",
"metallic", "underwater" and "there's a generator behind me" all describe. It is
the *denoiser*, not the room.

The fix is not a better denoiser, it's less of it. We run the denoiser on a
parallel branch and mix it back against the untouched signal:

    asplit -> [dry]-----------------\
           -> [wet] arnndn ---------> amix(weights=dry:wet)

At wet=1.0 you get today's artifacty sound. At wet=0.65 the noise drops most of
the way and the artifacts stop being audible, because the dry signal masks them.
That blend is what every commercial noise reducer exposes as its one big knob.


THE ECHO
--------
There is no dereverb filter in ffmpeg, but reverb has a property we can exploit:
the tail is always quieter than the sound that caused it. So a downward expander
— a gate that *attenuates* rather than mutes — turns the level down in the gaps
between words, where the tail lives, and leaves the words themselves alone.
That's the same principle behind the dereverb in most editing suites. It cannot
remove reflections that overlap the speech; it removes the ring-out after it,
which is the part you actually hear as "echoey room".

`range` keeps it musical: 0.5 means the gaps drop ~6 dB, not to silence. Gating
hard to silence sounds worse than the echo did — the noise floor pumping on and
off is more distracting than a constant one.


Loudness is done properly in two stages: an analysis pass that only decodes audio
(no encode), then the real render applies the measured values. Single-pass
loudnorm has to guess, and on quiet source it pumps.

Targets follow what short-form platforms normalise to:
    I  = -14 LUFS   integrated loudness
    TP = -1.5 dBTP  true peak ceiling (headroom for lossy re-encode downstream)
    LRA = 11 LU     loudness range
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

MODELS_DIR = Path(__file__).resolve().parent.parent / "vendor" / "arnndn-models"
# "bd" (beguiling drafter) is the general-purpose speech model and behaves best on
# room-recorded conversation; std is the conservative fallback.
PREFERRED_MODELS = ("bd.rnnn", "std.rnnn", "cb.rnnn")

TARGET_I = -14.0
TARGET_TP = -1.5
TARGET_LRA = 11.0

# Presets are starting points, not verdicts. Every number scales with `intensity`
# so a wrong guess is a dial, not a ruined clip.
#
#   denoise_wet  0..1  how much of the denoised branch survives the mix.
#                      Above ~0.8 the artifacts become audible on most rooms.
#   dereverb     0..1  downward-expansion depth in the gaps (0 = off)
#   presence     dB    lift at 3 kHz — consonants, intelligibility
#   warmth       dB    cut at 300 Hz — boxiness from a small room
#   top_hz       Hz    lowpass. Lower = less hiss but duller. 14k keeps it natural.
PRESETS = {
    "office": {
        "label": "Office / hard room",
        # small reflective room: the echo is the main complaint, noise is moderate
        "denoise_wet": 0.62, "dereverb": 0.55, "presence": 2.0,
        "warmth": -3.0, "top_hz": 14000, "hpf": 85,
    },
    "gym": {
        "label": "Gym / noisy",
        # loud constant background, big space: lean harder on noise, less on echo
        "denoise_wet": 0.80, "dereverb": 0.35, "presence": 2.5,
        "warmth": -2.0, "top_hz": 13000, "hpf": 100,
    },
    "quiet": {
        "label": "Quiet / treated room",
        # already clean: barely touch it, keep the top end
        "denoise_wet": 0.35, "dereverb": 0.20, "presence": 1.5,
        "warmth": -1.5, "top_hz": 15000, "hpf": 75,
    },
    "raw": {
        "label": "Level only",
        # no denoise, no dereverb — just make it loud enough. The honest baseline
        # to A/B against when a preset sounds processed.
        "denoise_wet": 0.0, "dereverb": 0.0, "presence": 0.0,
        "warmth": 0.0, "top_hz": 0, "hpf": 70,
    },
}

DEFAULT_PRESET = "office"


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def model_path() -> Optional[Path]:
    for name in PREFERRED_MODELS:
        p = MODELS_DIR / name
        if p.exists():
            return p
    return None


def _escape(p: Path) -> str:
    """ffmpeg filter args: Windows backslashes and the drive colon both bite."""
    return str(p).replace("\\", "/").replace(":", "\\:")


def measure_loudness(source: Path, start_s: float, dur_s: float,
                     pre_filters: str = "") -> Optional[dict]:
    """loudnorm analysis pass. Decodes audio only — no encode, no output file."""
    chain = (pre_filters + "," if pre_filters else "") + \
        f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json"
    cmd = [_ffmpeg(), "-hide_banner", "-nostats",
           "-ss", f"{start_s:.3f}", "-t", f"{dur_s:.3f}", "-i", str(source),
           "-map", "0:a:0", "-af", chain, "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900)
    except (OSError, subprocess.SubprocessError):
        return None
    txt = (p.stderr or "") + (p.stdout or "")
    # The JSON block is the last {...} ffmpeg prints.
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", txt, re.S)
    if not blocks:
        return None
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None


def _settings(preset: str, intensity: float, overrides: Optional[dict]) -> dict:
    p = dict(PRESETS.get(str(preset or "").lower(), PRESETS[DEFAULT_PRESET]))
    try:
        k = float(intensity)
    except (TypeError, ValueError):
        k = 1.0
    k = max(0.0, min(1.5, k))

    # Scale the *effects*, not the structural values (hpf/top_hz stay put).
    for key in ("denoise_wet", "dereverb", "presence", "warmth"):
        p[key] = p[key] * k
    p["denoise_wet"] = max(0.0, min(1.0, p["denoise_wet"]))
    p["dereverb"] = max(0.0, min(1.0, p["dereverb"]))
    p["_k"] = k

    # Per-clip overrides from the UI win over everything.
    for key, val in (overrides or {}).items():
        if key in p and val is not None:
            p[key] = val
    return p


def build_chain(source: Path, start_s: float, dur_s: float, *,
                denoise: bool = True, normalize: bool = True,
                preset: str = DEFAULT_PRESET, intensity: float = 1.0,
                overrides: Optional[dict] = None) -> tuple[str, dict]:
    """-> (ffmpeg -af chain, info). Safe to call even when nothing is available;
    it just returns a weaker chain rather than failing the render.

    The returned string may be a filtergraph with labels (denoise uses a parallel
    branch), but it always has exactly one input and one output, so it is still
    valid for `-af` and still safe to append `,loudnorm=...` to.
    """
    s = _settings(preset, intensity, overrides)
    info: dict = {"denoise": False, "normalize": False, "model": None,
                  "preset": preset, "intensity": s["_k"], "dereverb": 0.0}

    # Order matters. Clean it, de-echo it, shape it, control the dynamics, THEN
    # set the level. Normalisation alone just makes quiet noisy audio into loud
    # noisy audio — it lifts the noise floor along with the voice.
    head: list[str] = [f"highpass=f={int(s['hpf'])}"]
    if s["top_hz"]:
        head.append(f"lowpass=f={int(s['top_hz'])}")
    pre = ",".join(head)

    # --- denoise, as a parallel wet/dry blend (see module docstring) -----------
    wet = s["denoise_wet"]
    if denoise and wet > 0.02:
        m = model_path()
        if m:
            branch = f"arnndn=m='{_escape(m)}'"
            info.update(denoise=True, model=m.name)
        else:
            # No model on disk: FFT denoiser still helps, just less cleanly.
            branch = "afftdn=nf=-25"
            info.update(denoise=True, model="afftdn (no rnnn model found)")
        dry = 1.0 - wet
        # normalize=0 keeps the summed level intact instead of halving it.
        pre = (f"{pre},asplit=2[cm_dry][cm_wet];"
               f"[cm_wet]{branch}[cm_den];"
               f"[cm_dry][cm_den]amix=inputs=2:weights={dry:.3f} {wet:.3f}:normalize=0")
        info["denoise_wet"] = round(wet, 3)

    # --- dereverb: downward expansion in the gaps ------------------------------
    dr = s["dereverb"]
    if dr > 0.02:
        # threshold rises with depth so more of the tail falls below it;
        # range shrinks so the attenuation gets deeper. Slow-ish release
        # (~180 ms) so it ducks the tail without chattering between syllables.
        thresh = 0.010 + 0.030 * dr          # ~-40 dBFS .. ~-28 dBFS
        rng = max(0.10, 1.0 - 0.85 * dr)     # 1.0 = untouched, 0.15 = -16 dB
        pre += (f",agate=threshold={thresh:.4f}:ratio=2:attack=8"
                f":release=180:range={rng:.3f}:makeup=1")
        info["dereverb"] = round(dr, 3)

    # --- shaping, after cleanup so the denoiser saw the raw voice --------------
    if abs(s["warmth"]) > 0.1:
        pre += f",equalizer=f=300:t=q:w=1.2:g={s['warmth']:.1f}"   # boxiness
    if abs(s["presence"]) > 0.1:
        pre += f",equalizer=f=3000:t=q:w=1.5:g={s['presence']:.1f}"  # consonants
        pre += ",deesser=i=0.4"    # only needed because presence exaggerates S
    # Gentle ~3:1 — evens loud/quiet delivery without pushing the floor up.
    pre += ",compand=attacks=0.02:decays=0.3:points=-70/-70|-40/-28|-20/-14|0/-8"
    info["shaped"] = True

    if not normalize:
        return pre, info

    meas = measure_loudness(source, start_s, dur_s, pre)
    if meas:
        try:
            # Linear mode with measured values = accurate one-shot gain, no pumping.
            norm = (f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}"
                    f":measured_I={float(meas['input_i'])}"
                    f":measured_TP={float(meas['input_tp'])}"
                    f":measured_LRA={float(meas['input_lra'])}"
                    f":measured_thresh={float(meas['input_thresh'])}"
                    f":offset={float(meas.get('target_offset', 0.0))}"
                    f":linear=true:print_format=summary")
            info.update(normalize=True, measured_i=float(meas["input_i"]),
                        measured_tp=float(meas["input_tp"]), mode="two-pass linear")
        except (KeyError, TypeError, ValueError):
            norm = f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}"
            info.update(normalize=True, mode="single-pass fallback")
    else:
        # Measurement failed (odd codec, no audio) - dynamic mode still lifts it.
        norm = f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}"
        info.update(normalize=True, mode="single-pass fallback")

    return f"{pre},{norm}", info


def describe(info: dict) -> str:
    bits = []
    if info.get("denoise"):
        bits.append(f"denoise {int(info.get('denoise_wet', 0) * 100)}% ({info.get('model')})")
    if info.get("dereverb"):
        bits.append(f"dereverb {int(info['dereverb'] * 100)}%")
    if info.get("shaped"):
        bits.append("voice chain")
    if info.get("normalize"):
        if "measured_i" in info:
            bits.append(f"loudness {info['measured_i']:.1f} -> {TARGET_I} LUFS")
        else:
            bits.append(f"loudness -> {TARGET_I} LUFS ({info.get('mode')})")
    return ", ".join(bits) or "audio untouched"
