"""Named style presets — save a look once, apply it with one tap.

The settings on the Upload page are ONE current style. That works until you
shoot in two places: an office grade flatters a hard warm room and wrecks a gym,
and a trainer's clips want a different caption colour from your own. Re-dialling
six controls every time is how people stop bothering and ship inconsistent work.

So: name a look, click it later. A preset captures everything that decides how a
clip comes out —

    colour   preset + strength
    voice    preset + strength + volume
    aspect   ratio + crop position + zoom
    captions size, height, outline, colour, x, width

Applying one writes those straight into look-settings.json and
caption-settings.json, which is what the renderer already reads. There is no
second source of truth and nothing to keep in sync — a preset is a saved copy of
the real settings, not a parallel system.

Built-ins are starting points, not decisions: they exist so a new environment is
one tap away from watchable rather than a blank slate. Every one is editable and
deletable, and saving over a built-in name just replaces it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "presets.json"

# What a preset is allowed to carry. Anything outside this is ignored on save,
# so a malformed POST cannot smuggle unknown keys into the render settings.
LOOK_KEYS = ("color_preset", "color_intensity", "audio_preset", "audio_intensity",
             "volume_db", "aspect_ratio", "aspect_pan", "aspect_panx", "aspect_zoom")
CAP_KEYS = ("size_wide", "margin_wide", "outline", "color", "outline_color",
            "x", "width")

BUILTIN = {
    "Office": {
        "note": "Hard warm room, two people at a table.",
        "look": {"color_preset": "office", "color_intensity": 1.0,
                 "audio_preset": "office", "audio_intensity": 1.0, "volume_db": 0.0,
                 "aspect_ratio": "4:5", "aspect_pan": 0.62, "aspect_panx": 0.5,
                 "aspect_zoom": 1.0},
        "captions": {"size_wide": 20, "margin_wide": 70, "outline": 4,
                     "color": "#FFFFFF", "x": 0.5, "width": 0.88},
    },
    "Gym": {
        "note": "Fluorescent green cast, louder background.",
        "look": {"color_preset": "gym", "color_intensity": 1.0,
                 "audio_preset": "gym", "audio_intensity": 1.0, "volume_db": 0.0,
                 "aspect_ratio": "9:16", "aspect_pan": 0.5, "aspect_panx": 0.5,
                 "aspect_zoom": 1.0},
        "captions": {"size_wide": 22, "margin_wide": 90, "outline": 5,
                     "color": "#FFFFFF", "x": 0.5, "width": 0.9},
    },
    "Bold caption": {
        "note": "Same grade, captions that carry on a muted feed.",
        "look": {"color_preset": "office", "color_intensity": 1.0,
                 "audio_preset": "office", "audio_intensity": 1.0, "volume_db": 0.0,
                 "aspect_ratio": "4:5", "aspect_pan": 0.62, "aspect_panx": 0.5,
                 "aspect_zoom": 1.0},
        "captions": {"size_wide": 30, "margin_wide": 80, "outline": 6,
                     "color": "#F5A524", "x": 0.5, "width": 0.8},
    },
}


def _clean(src: dict, keys: tuple) -> dict:
    return {k: src[k] for k in keys if k in src}


def load() -> dict:
    saved = {}
    try:
        saved = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    # User presets win on a name clash — saving over "Office" should replace it.
    out = {k: {**v, "builtin": True} for k, v in BUILTIN.items()}
    out.update(saved)
    return out


def save(name: str, look: dict, captions: dict, note: str = "") -> dict:
    name = str(name or "").strip()[:40]
    if not name:
        raise ValueError("a preset needs a name")
    saved = {}
    try:
        saved = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    saved[name] = {"note": str(note or "")[:120],
                   "look": _clean(look or {}, LOOK_KEYS),
                   "captions": _clean(captions or {}, CAP_KEYS),
                   "saved_at": time.time()}
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return load()


def delete(name: str) -> dict:
    try:
        saved = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    saved.pop(str(name), None)
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return load()


def apply(name: str) -> Optional[dict]:
    """Write a preset into the live settings. -> what is now active, or None."""
    p = load().get(str(name))
    if not p:
        return None
    from . import captions as _cap, look as _look  # noqa: PLC0415
    _look.save(_clean(p.get("look") or {}, LOOK_KEYS))
    _cap.save(_clean(p.get("captions") or {}, CAP_KEYS))
    return {"look": _look.load(), "captions": _cap.load()}


def current(look: dict, captions: dict) -> Optional[str]:
    """Which preset the live settings match, if any. Lets the UI show what is
    active rather than leaving every button looking equally unselected."""
    for name, p in load().items():
        want_l = _clean(p.get("look") or {}, LOOK_KEYS)
        want_c = _clean(p.get("captions") or {}, CAP_KEYS)
        if all(str(look.get(k)) == str(v) for k, v in want_l.items()) and \
           all(str(captions.get(k)) == str(v) for k, v in want_c.items()):
            return name
    return None
