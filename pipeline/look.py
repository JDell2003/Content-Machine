"""Look & sound settings that live in a file, not in .env.

.env is read once at import, so changing a preset there means restarting the
server — which on this machine means killing an elevated scheduled task. These
settings get read fresh on every render instead, so a change on the phone
applies to the very next clip.

Precedence, narrowest first:
    job["look"]  ->  this file  ->  .env defaults  ->  hardcoded
"""
from __future__ import annotations

import json
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / "data" / "look-settings.json"

KEYS = {
    "color_preset": str, "color_intensity": float,
    "audio_preset": str, "audio_intensity": float,
    "volume_db": float,
    # Aspect belongs here too. Without it, "use this style for all" silently
    # dropped the ratio — every new clip came out in the source shape no matter
    # what you had chosen.
    "aspect_ratio": str, "aspect_pan": float, "aspect_panx": float,
    "aspect_zoom": float,
    "outro": bool,
}


def defaults() -> dict:
    from server import config as cfg  # noqa: PLC0415 - avoids an import cycle
    return {
        "color_preset": getattr(cfg, "COLOR_PRESET", "office"),
        "color_intensity": float(getattr(cfg, "COLOR_INTENSITY", 1.0)),
        "audio_preset": getattr(cfg, "AUDIO_PRESET", "office"),
        "audio_intensity": float(getattr(cfg, "AUDIO_INTENSITY", 1.0)),
        "volume_db": 0.0,
        "aspect_ratio": "original",
        "aspect_pan": 0.62,      # captions sit low; a centred crop clips them
        "aspect_panx": 0.5,
        "aspect_zoom": 1.0,
        "outro": True,
    }


def load() -> dict:
    s = defaults()
    try:
        s.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return s


def save(patch: dict) -> dict:
    s = load()
    for key, cast in KEYS.items():
        if key not in (patch or {}):
            continue
        val = patch[key]
        try:
            s[key] = cast(val) if cast is not bool else bool(val)
        except (TypeError, ValueError):
            continue
    # Clamp here rather than trusting the caller — this file is written from a
    # phone form and read straight into an ffmpeg filter string.
    for k in ("color_intensity", "audio_intensity"):
        s[k] = max(0.0, min(1.5, float(s[k])))
    for k in ("aspect_pan", "aspect_panx"):
        s[k] = max(0.0, min(1.0, float(s[k])))
    s["aspect_zoom"] = max(1.0, min(3.0, float(s["aspect_zoom"])))
    s["volume_db"] = max(-24.0, min(24.0, float(s["volume_db"])))
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s
