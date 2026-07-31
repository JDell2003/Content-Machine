"""Manual reframing — pick the aspect ratio and drag the crop where you want it.

Why this is manual and not automatic: the auto face-tracker was removed because
it framed the wall between two people. On a two-person interview shot in
portrait, there is no correct answer a detector can find — sometimes the shot is
about the person talking, sometimes about the other one reacting. You know which.
The machine's job is to make choosing it take three seconds.

What it fixes on the current footage: a portrait phone video of two people at a
table wastes the top third on ceiling and the bottom third on tabletop. Cropping
to 4:5 or 1:1 around the faces throws away nothing that matters and roughly
doubles how large the subjects are in frame.

QUALITY NOTE — this re-encodes the finished clip, so it is the one place the
machine breaks its own "no intermediate re-encode" rule. The alternative is
re-running the full render (~45s and it re-burns the captions at the new size),
which is better quality but too slow to iterate against. So: crop is instant and
costs one high-bitrate encode; if you want it perfect, re-render instead.

CAPTIONS are already burned in, so a crop can cut them off. `caption_safe`
reports whether the proposed box keeps the caption band (the lower-middle third,
where write_karaoke_srt puts it), and the UI warns before saving.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

# label -> (w, h). "original" means leave the frame alone.
ASPECTS = {
    "9:16": (9, 16),     # Reels / TikTok / Shorts
    "4:5": (4, 5),       # Instagram feed — the largest a post can be in-feed
    "1:1": (1, 1),       # square, safe everywhere
    "16:9": (16, 9),     # YouTube / landscape
    "original": None,
}


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def probe(path: Path) -> tuple[int, int]:
    """-> (width, height) as displayed. ffprobe already applies rotation here."""
    out = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error",
         "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True, timeout=60).stdout.strip()
    w, _, h = out.partition("x")
    return int(w), int(h)


def plan(src_w: int, src_h: int, aspect: str = "4:5", zoom: float = 1.0,
         cx: float = 0.5, cy: float = 0.5) -> Optional[dict]:
    """Work out the crop rectangle.

    zoom 1.0 = the biggest box of this aspect that fits. Above 1.0 tightens in.
    cx/cy are 0..1 and place the box's centre; they're clamped so the box can
    never run off the edge of the frame (ffmpeg would just error).
    """
    ratio = ASPECTS.get(aspect)
    if ratio is None:
        return None

    ar = ratio[0] / ratio[1]
    # Largest box of this aspect that fits inside the source.
    if src_w / src_h > ar:
        box_h = src_h
        box_w = box_h * ar
    else:
        box_w = src_w
        box_h = box_w / ar

    z = max(1.0, min(3.0, float(zoom or 1.0)))
    box_w /= z
    box_h /= z

    # Even dimensions — H.264 chroma subsampling requires it.
    cw = max(16, int(box_w) // 2 * 2)
    ch = max(16, int(box_h) // 2 * 2)

    x = int(cx * src_w - cw / 2)
    y = int(cy * src_h - ch / 2)
    x = max(0, min(src_w - cw, x)) // 2 * 2
    y = max(0, min(src_h - ch, y)) // 2 * 2

    return {"w": cw, "h": ch, "x": x, "y": y,
            "src_w": src_w, "src_h": src_h, "aspect": aspect,
            "zoom": round(z, 3), "cx": round(cx, 4), "cy": round(cy, 4)}


def caption_safe(box: dict) -> bool:
    """Captions sit in a band across the lower-middle of the frame. If the crop
    doesn't cover that band, the burned-in text gets sliced."""
    band_top = box["src_h"] * 0.55
    band_bottom = box["src_h"] * 0.88
    return box["y"] <= band_top and (box["y"] + box["h"]) >= band_bottom


# Delivery sizes. Cropping then scaling to a standard size means every platform
# gets what it expects instead of an odd resolution it will re-encode itself.
DELIVERY = {"9:16": (1080, 1920), "4:5": (1080, 1350),
            "1:1": (1080, 1080), "16:9": (1920, 1080)}


def apply(src: Path, dest: Path, box: dict, *, nvenc: bool = True) -> Path:
    """Crop and write. Audio is copied — it never needs re-encoding for a crop."""
    vf = [f"crop={box['w']}:{box['h']}:{box['x']}:{box['y']}"]
    target = DELIVERY.get(box["aspect"])
    if target:
        # force_original_aspect_ratio isn't needed: the crop already matches.
        vf.append(f"scale={target[0]}:{target[1]}:flags=lanczos")

    if nvenc:
        codec = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                 "-cq", "19", "-b:v", "0", "-profile:v", "high"]
    else:
        codec = ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                 "-profile:v", "high"]

    args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
            "-vf", ",".join(vf), *codec, "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-c:a", "copy", str(dest)]
    p = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    if p.returncode != 0 and nvenc:
        return apply(src, dest, box, nvenc=False)   # driver/session limit
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "")[-400:] or "crop failed")
    return dest
