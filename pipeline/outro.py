"""The CTA outro — your VSL, welded onto the end of every clip.

The point: someone watches a clip about hooks, likes it, and then immediately
gets told what you actually do. They don't have to go looking. Every clip ends
the same way, so volume compounds into recognition instead of just impressions.

HOW THIS AVOIDS A SECOND ENCODE
-------------------------------
The obvious way to append B to A is `concat` filter, which re-encodes both. That
would put a second generation of compression on every clip and break the rule the
whole renderer is built around.

Instead we use the concat *demuxer* with `-c copy`, which splices the two
bitstreams without touching a single frame. That only works if both files agree
on codec, resolution, pixel format, frame rate, and audio layout — so we bake the
outro once per geometry to match exactly what the renderer produces, and cache
it. The result:

    clip   : 1 encode (from source, as always)
    outro  : 1 encode, ONCE, reused by every clip forever after
    joining: 0 encodes

The bake also applies the same colour grade and voice chain the clips get, so the
CTA doesn't look and sound like it was filmed on a different planet.

If the stream copy is refused (encoder parameter sets that a player might choke
on), we fall back to a filter concat and say so — a working clip with one extra
generation beats no clip.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "data" / "assets"
CACHE = ASSETS / "outro-cache"
SOURCE = ASSETS / "outro_source.mp4"
META = ASSETS / "outro.json"

MAX_OUTRO_S = 90.0     # a CTA longer than this stops being a CTA


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


# ------------------------------------------------------------------ the asset
def info() -> dict:
    """What's currently installed, for the UI."""
    if not SOURCE.exists():
        return {"present": False}
    try:
        meta = json.loads(META.read_text(encoding="utf-8")) if META.exists() else {}
    except (OSError, json.JSONDecodeError):
        meta = {}
    return {"present": True, "size_mb": round(SOURCE.stat().st_size / 1e6, 1),
            "duration_s": meta.get("duration_s"), "name": meta.get("name"),
            "uploaded_at": meta.get("uploaded_at"),
            "baked": len(list(CACHE.glob("*.mp4"))) if CACHE.exists() else 0}


def probe(path: Path) -> dict:
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate:format=duration",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=120).stdout
    try:
        d = json.loads(out)
        st = (d.get("streams") or [{}])[0]
        num, _, den = str(st.get("r_frame_rate", "30/1")).partition("/")
        fps = float(num) / float(den or 1)
        return {"w": int(st.get("width") or 0), "h": int(st.get("height") or 0),
                "fps": round(fps, 3),
                "duration_s": float((d.get("format") or {}).get("duration") or 0)}
    except (json.JSONDecodeError, ValueError, ZeroDivisionError):
        return {"w": 0, "h": 0, "fps": 30.0, "duration_s": 0.0}


def install(temp_file: Path, original_name: str = "") -> dict:
    """Adopt an uploaded file as the outro. Clears the bake cache — every cached
    variant belongs to the old video and would silently keep playing."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    meta = probe(temp_file)
    if meta["duration_s"] > MAX_OUTRO_S:
        raise ValueError(f"outro is {meta['duration_s']:.0f}s; keep it under "
                         f"{MAX_OUTRO_S:.0f}s or it will bury the clip")
    if not meta["w"] or not meta["h"]:
        raise ValueError("that file has no readable video stream")

    clear_cache()
    shutil.move(str(temp_file), str(SOURCE))
    import time  # noqa: PLC0415
    payload = {**meta, "name": original_name or temp_file.name, "uploaded_at": time.time()}
    META.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def remove() -> None:
    clear_cache()
    SOURCE.unlink(missing_ok=True)
    META.unlink(missing_ok=True)


def clear_cache() -> None:
    if CACHE.exists():
        shutil.rmtree(CACHE, ignore_errors=True)


# ------------------------------------------------------------------ the bake
def _key(w: int, h: int, fps: float, grade: str, afilter: str,
         fade_in: float = 0.0) -> str:
    h_ = hashlib.sha1(f"{grade}|{afilter}|{fade_in}".encode()).hexdigest()[:10]
    return f"{w}x{h}_{fps:.3f}_{h_}.mp4"


def bake(w: int, h: int, fps: float, *, grade: str = "", afilter: str = "",
         codec_args: Optional[list[str]] = None,
         audio_args: Optional[list[str]] = None,
         fade_in: float = 0.0) -> Optional[Path]:
    """Encode the outro to EXACTLY the renderer's output format, once, cached.

    Letterboxes rather than crops: the CTA is you talking to camera, and cropping
    a talking head to fit a different aspect is how you cut someone's forehead off.
    """
    if not SOURCE.exists():
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / _key(w, h, fps, grade, afilter, fade_in)
    if dest.exists():
        return dest

    vf = [f"scale={w}:{h}:force_original_aspect_ratio=decrease",
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
          "setsar=1", f"fps={fps:.3f}"]
    if grade:
        vf.append(grade)
    # Fade LAST so it acts on the finished picture, not on an intermediate.
    if fade_in > 0.01:
        vf.append(f"fade=t=in:st=0:d={fade_in:.3f}")

    args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(SOURCE),
            "-vf", ",".join(vf)]
    af = afilter
    if fade_in > 0.01:
        # The voice fades with the picture; a hard audio cut is more jarring
        # than a hard visual one.
        af = f"{af},afade=t=in:st=0:d={fade_in:.3f}" if af else              f"afade=t=in:st=0:d={fade_in:.3f}"
    if af:
        args += ["-af", af]
    args += [*(codec_args or ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                              "-pix_fmt", "yuv420p", "-profile:v", "high"]),
             "-movflags", "+faststart",
             *(audio_args or ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]),
             "-ar", "48000", str(dest)]
    p = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    if p.returncode != 0:
        dest.unlink(missing_ok=True)
        return None
    return dest


# ------------------------------------------------------------------ the join
def append(clip: Path, baked: Path) -> tuple[Path, str]:
    """Splice baked onto the end of clip, in place. -> (path, how it was done)."""
    clip = clip.resolve()
    baked = baked.resolve()
    listing = clip.with_name(clip.stem + "_concat.txt")
    # Two traps in this manifest format:
    #  * paths resolve RELATIVE TO THE MANIFEST's directory, so they must be
    #    absolute or ffmpeg looks for data/tmp/data/tmp/clip.mp4
    #  * single quote is the escape character, so a quote in a path needs '\''
    def _entry(p: Path) -> str:
        return "file '" + p.as_posix().replace("'", r"'\''") + "'"
    listing.write_text(f"{_entry(clip)}\n{_entry(baked)}\n", encoding="utf-8")
    joined = clip.with_name(clip.stem + "_cta.mp4")

    copy_args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", str(listing),
                 "-c", "copy", "-movflags", "+faststart", str(joined)]
    p = subprocess.run(copy_args, capture_output=True, text=True, timeout=1800)
    how = "stream copy (no re-encode)"

    if p.returncode != 0 or not joined.exists():
        # Fallback: filter concat. Costs one generation on the clip; still better
        # than shipping without the CTA.
        joined.unlink(missing_ok=True)
        re_args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                   "-i", str(clip), "-i", str(baked),
                   "-filter_complex",
                   "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
                   "-map", "[v]", "-map", "[a]",
                   "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                   "-movflags", "+faststart", str(joined)]
        p2 = subprocess.run(re_args, capture_output=True, text=True, timeout=3600)
        if p2.returncode != 0:
            listing.unlink(missing_ok=True)
            raise RuntimeError((p2.stderr or "")[-400:] or "outro append failed")
        how = "re-encoded (stream copy refused)"

    listing.unlink(missing_ok=True)
    # Replace the original so every downstream path (review, download, reframe)
    # picks it up with no extra bookkeeping.
    clip.unlink(missing_ok=True)
    joined.replace(clip)
    return clip, how
