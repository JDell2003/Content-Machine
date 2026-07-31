"""Caption style, spelling corrections, and per-clip text editing.

Two different problems live here, and they want opposite solutions:

  SPELLING is a whole-machine problem. Whisper will mis-hear "Etavis" the same
  way in every video you ever record. Fixing it on one clip fixes one clip;
  putting it in the corrections dictionary fixes it retroactively on re-render
  AND on every future video. So corrections are global by default.

  WORDING is a per-clip problem. Sometimes a line just reads badly burned in.
  That edits one clip's .srt and re-renders only that clip.

STYLE (size, vertical position, outline) is global with a per-job override,
because you'll settle on one look and want it everywhere — but a clip shot in a
different aspect sometimes needs the text moved.

COST HONESTY: burned-in text is pixels. There is no way to change it without
re-encoding the clip. A single clip re-render is ~45s. "Apply to all" on a
60-clip job is real time — the UI says so before you press it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "data" / "caption-settings.json"

DEFAULTS = {
    # Sized for a phone at arm's length. Fontsize is in ASS points against the
    # video height, so these are tuned per aspect rather than shared.
    "size_wide": 20,
    "size_vertical": 17,
    "margin_wide": 70,        # distance from the bottom edge
    "margin_vertical": 320,   # 9:16 is 1920 tall; platform UI eats the bottom
    "outline": 4,
    "bold": True,
    "uppercase": True,
    "group": 4,               # words per caption card
    "corrections": {},        # {"wrong": "right"} applied case-insensitively
}


def load() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    s["corrections"] = {str(k): str(v) for k, v in (s.get("corrections") or {}).items()}
    return s


def save(patch: dict) -> dict:
    s = load()
    for key, val in (patch or {}).items():
        if key not in DEFAULTS:
            continue
        if key == "corrections":
            # Keys are matched case-insensitively later; store them lowercased so
            # the dictionary can't hold two entries that mean the same thing.
            s[key] = {str(k).strip().lower(): str(v).strip()
                      for k, v in (val or {}).items() if str(k).strip()}
        elif isinstance(DEFAULTS[key], bool):
            s[key] = bool(val)
        elif isinstance(DEFAULTS[key], int):
            s[key] = max(1, int(val))
        else:
            s[key] = val
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


# ------------------------------------------------------------- corrections
def apply_corrections(text: str, corrections: Optional[dict] = None) -> str:
    """Whole-word, case-insensitive replacement.

    Word-boundary anchored so a correction for "ab" can't corrupt "abs" — which
    matters a lot in fitness vocabulary.
    """
    corr = corrections if corrections is not None else load()["corrections"]
    if not corr or not text:
        return text
    for wrong, right in corr.items():
        if not wrong:
            continue
        text = re.sub(rf"\b{re.escape(wrong)}\b", right, text, flags=re.IGNORECASE)
    return text


# ------------------------------------------------------------------- style
def force_style(vertical: bool, override: Optional[dict] = None) -> str:
    """The ASS style string handed to ffmpeg's subtitles filter."""
    s = load()
    s.update({k: v for k, v in (override or {}).items() if v is not None})
    size = s["size_vertical"] if vertical else s["size_wide"]
    margin = s["margin_vertical"] if vertical else s["margin_wide"]
    return (f"FontName=Arial Black,Fontsize={int(size)},Bold={1 if s['bold'] else 0},"
            f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,"
            f"Outline={int(s['outline'])},Shadow=1,Alignment=2,MarginV={int(margin)}")


# --------------------------------------------------------------- srt editing
_TIME = re.compile(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})")


def read_srt(path: Path) -> list[dict]:
    """-> [{index, start, end, text}] so the UI can show editable lines."""
    if not path or not path.exists():
        return []
    out, block = [], []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines() + [""]:
        if raw.strip() == "":
            if len(block) >= 3:
                m = _TIME.search(block[1])
                if m:
                    out.append({"index": len(out) + 1, "start": m.group(1),
                                "end": m.group(2), "text": "\n".join(block[2:]).strip()})
            block = []
        else:
            block.append(raw)
    return out


def write_srt(path: Path, cues: list[dict]) -> Path:
    lines = []
    for i, cue in enumerate(cues, start=1):
        text = str(cue.get("text") or "").strip()
        if not text:
            continue   # an emptied line means "drop this caption"
        lines += [str(i), f"{cue['start']} --> {cue['end']}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
