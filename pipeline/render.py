"""Rendering — single pass from source, always.

The vendored SamurAIGPT clipper re-encodes twice (libx264 crf20 for the cut, then
OpenCV mp4v for the vertical reframe). That's a double transcode through a weak
codec and it is why this module exists instead: analyse with OpenCV, then let ONE
ffmpeg pass read the ORIGINAL file and write the final clip.

  16:9 : one encode from source at source resolution, high bitrate
  9:16 : same, plus a crop whose x position follows the faces over time

Seeking uses -ss before -i (fast, keyframe-accurate seek) plus -ss after -i
(exact trim), which avoids decoding an hour of video to reach minute 50.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

# crf 16 + slow is visually transparent for talking-head footage and still lands
# well under platform ingest limits at 1080p.
CRF = "16"
PRESET = "slow"


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


_DIMS: dict[str, tuple[int, int]] = {}


def probe_dims(source: Path) -> tuple[int, int]:
    """Source width/height, cached. Needed so crop maths happens in Python
    instead of in an ffmpeg expression."""
    key = str(source)
    if key in _DIMS:
        return _DIMS[key]
    exe = shutil.which("ffprobe") or "ffprobe"
    try:
        p = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0:s=x", str(source)],
            capture_output=True, text=True, timeout=120)
        w, h = (int(x) for x in p.stdout.strip().split("x")[:2])
    except (OSError, ValueError, subprocess.SubprocessError):
        w, h = 0, 0
    _DIMS[key] = (w, h)
    return w, h


def _run(args: list[str], timeout: int = 3600) -> None:
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {(p.stderr or '')[-500:]}")


def _seek(source: Path, start_s: float, dur_s: float) -> list[str]:
    pre = max(0.0, start_s - 2.0)  # coarse seek 2s early, then trim exactly
    return ["-ss", f"{pre:.3f}", "-i", str(source),
            "-ss", f"{start_s - pre:.3f}", "-t", f"{dur_s:.3f}"]


def _audio_args() -> list[str]:
    # Re-encoding audio at 192k AAC is inaudible next to a re-cut video and
    # sidesteps container/timestamp problems that -c:a copy causes on trims.
    return ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]


def render_wide(source: Path, out: Path, start_s: float, end_s: float,
                punch_ins: Optional[list[dict]] = None) -> Path:
    """16:9 at source resolution, one encode from the original file."""
    dur = max(0.2, end_s - start_s)
    vf = _punch_filter(punch_ins, dur) if punch_ins else None
    args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *_seek(source, start_s, dur)]
    if vf:
        args += ["-vf", vf]
    args += ["-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", *_audio_args(), str(out)]
    _run(args)
    return out


def _punch_filter(punch_ins: list[dict], dur: float, zoom: float = 1.10) -> str:
    """Subtle emphasis zoom at the peak lines the ranker identified.

    scale+crop with time-gated expressions rather than zoompan: zoompan re-times
    frames and fights variable frame rates, while a gated crop is stable and
    cheap. Capped at 3 punch-ins, each ~1.8s, easing handled by the hold length
    rather than per-frame interpolation (subtle over flashy).
    """
    windows = []
    for p in (punch_ins or [])[:3]:
        try:
            t = float(p.get("t_rel_s"))
        except (TypeError, ValueError):
            continue
        if 0 <= t <= max(0.0, dur - 0.5):
            # ~2s hold: long enough to register as emphasis, short enough not to
            # feel like a different shot. Standard practice is subtle and brief.
            windows.append((max(0.0, t - 0.3), min(dur, t + 1.8)))
    if not windows:
        return ""
    # Commas escaped: ffmpeg splits filter args on unescaped commas.
    gate = "+".join(f"between(t\\,{a:.2f}\\,{b:.2f})" for a, b in windows)
    z = f"(1+({zoom - 1:.3f})*gte({gate}\\,1))"
    return (f"scale=iw*{z}:ih*{z}:eval=frame,"
            f"crop=iw/{z}:ih/{z}:(iw-iw/{z})/2:(ih-ih/{z})/2")


def silence_cuts(source: Path, start_s: float, end_s: float, *,
                 noise_db: int = -32, min_silence_s: float = 0.45,
                 keep_pad_s: float = 0.12) -> list[tuple[float, float]]:
    """Find the KEEP ranges (clip-relative) after removing dead air.

    Standard talking-head practice: cut the gaps, not the words. Pads each side of
    a cut so consonants aren't clipped, and ignores very short pauses which are
    normal speech rhythm rather than dead air.
    """
    dur = max(0.2, end_s - start_s)
    exe = _ffmpeg()
    try:
        p = subprocess.run(
            [exe, "-hide_banner", "-nostats", "-ss", f"{start_s:.3f}", "-t", f"{dur:.3f}",
             "-i", str(source), "-map", "0:a:0", "-af",
             f"silencedetect=noise={noise_db}dB:d={min_silence_s}", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except (OSError, subprocess.SubprocessError):
        return [(0.0, dur)]
    txt = (p.stderr or "") + (p.stdout or "")

    silences: list[tuple[float, float]] = []
    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[0-9.]+)", txt)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", txt)]
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else dur
        silences.append((max(0.0, s), min(dur, e)))

    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in silences:
        a = max(cursor, 0.0)
        b = min(s + keep_pad_s, dur)
        if b - a > 0.25:
            keeps.append((a, b))
        cursor = max(cursor, e - keep_pad_s)
    if dur - cursor > 0.25:
        keeps.append((max(0.0, cursor), dur))
    return keeps or [(0.0, dur)]


def face_track(source: Path, start_s: float, end_s: float, *, samples: int = 24) -> list[dict]:
    """Sample frames and find where the faces are. Analysis only — writes nothing.
    Returns [{t_rel, cx_frac}] centre-x as a fraction of width."""
    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        return []
    # OpenCV 5.x moved the Haar detector; probe rather than assume, and degrade to
    # "no track" (which skips 9:16) instead of raising into the render path.
    cascade_cls = getattr(cv2, "CascadeClassifier", None)
    if cascade_cls is None:
        objdetect = getattr(cv2, "objdetect", None)
        cascade_cls = getattr(objdetect, "CascadeClassifier", None) if objdetect else None
    haar_dir = getattr(getattr(cv2, "data", None), "haarcascades", None)
    if cascade_cls is None or not haar_dir:
        return []
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return []
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1
        cascade = cascade_cls(haar_dir + "haarcascade_frontalface_default.xml")
        if hasattr(cascade, "empty") and cascade.empty():
            return []
        dur = max(0.2, end_s - start_s)
        out = []
        for i in range(samples):
            t = start_s + dur * (i / max(1, samples - 1))
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.15, 5, minSize=(60, 60))
            if len(faces) == 0:
                continue
            # Midpoint between the outermost faces keeps both people in frame when
            # two are talking, instead of snapping to whoever detects strongest.
            lo = min(int(x) for x, _, _, _ in faces)
            hi = max(int(x) + int(fw) for x, _, fw, _ in faces)
            # Vertical position and face size matter as much as x: without them the
            # 9:16 crop keeps full height and fills the frame with ceiling.
            eye_y = min(int(y) + int(fh) * 0.42 for _, y, _, fh in faces)
            big = max(int(fh) for _, _, _, fh in faces)
            out.append({"t_rel": round(dur * (i / max(1, samples - 1)), 2),
                        "cx_frac": round(((lo + hi) / 2.0) / w, 4),
                        "eye_frac": round(eye_y / max(1, h), 4),
                        "face_frac": round(big / max(1, h), 4),
                        "faces": int(len(faces))})
        return out
    finally:
        cap.release()


def render_vertical(source: Path, out: Path, start_s: float, end_s: float, *,
                    track: Optional[list[dict]] = None,
                    punch_ins: Optional[list[dict]] = None,
                    subs_path: Optional[Path] = None) -> Optional[Path]:
    """9:16 from source in one pass. Crop x follows the face track. Returns None
    when no faces were found anywhere (no point guessing a crop)."""
    dur = max(0.2, end_s - start_s)
    track = track if track is not None else face_track(source, start_s, end_s)
    if not track:
        return None

    # Smooth the path so the frame drifts instead of snapping shot-to-shot.
    smoothed: list[tuple[float, float]] = []
    prev = track[0]["cx_frac"]
    for p in track:
        prev = prev + 0.35 * (p["cx_frac"] - prev)
        smoothed.append((p["t_rel"], prev))

    # Crop box computed numerically from the real source dimensions rather than
    # with min()/max() in the filter string. ffmpeg splits filter arguments on
    # commas, so every comma inside a function call has to be escaped as "\,";
    # doing the arithmetic here keeps the expression far simpler and this bug
    # (No such filter: 'ih*9/16):min(ih') from recurring.
    sw, sh = probe_dims(source)
    if not sw or not sh:
        return None

    # Frame height from face size, not the whole source. A wide room shot cropped
    # at full height is mostly ceiling; standard practice frames a talking head so
    # the head is roughly a third of frame height. face_frac*sh is the face; ~5.5x
    # that gives head-and-shoulders with air.
    face_frac = max(0.02, sum(p.get("face_frac", 0.08) for p in track) / len(track))
    crop_h = int(round(min(sh, max(sh * 0.34, face_frac * sh * 5.5))))
    crop_w = int(round(crop_h * 9 / 16))
    if crop_w > sw:                       # narrow source: fall back to full width
        crop_w = sw
        crop_h = int(round(crop_w * 16 / 9))
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    max_x, max_y = max(0, sw - crop_w), max(0, sh - crop_h)

    # Rule of thirds, both axes: eyes about a third down, subject a third across.
    def box_for(cx_frac: float, eye_frac: float) -> tuple[int, int]:
        x = cx_frac * sw - (crop_w / 3.0 if cx_frac < 0.5 else 2 * crop_w / 3.0)
        y = eye_frac * sh - crop_h / 3.0
        return (int(max(0, min(max_x, round(x)))), int(max(0, min(max_y, round(y)))))

    eyes = [p.get("eye_frac", 0.35) for p in track]
    eye_avg = sum(eyes) / len(eyes)        # hold y steady; a bobbing frame is worse
    parts = []
    for i, (t, cx) in enumerate(smoothed):
        nxt = smoothed[i + 1][0] if i + 1 < len(smoothed) else dur + 1
        parts.append((t, nxt, *box_for(cx, eye_avg)))
    x_expr, y_fixed = str(parts[-1][2]), parts[-1][3]
    for _t, nxt, x, _y in reversed(parts[:-1]):
        x_expr = f"if(lt(t\\,{nxt:.2f})\\,{x}\\,{x_expr})"

    # Crop then scale to a standard 1080x1920 so every clip is the same shape.
    vf = [f"crop={crop_w}:{crop_h}:{x_expr}:{y_fixed}", "scale=1080:1920:flags=lanczos"]
    if punch_ins:
        pf = _punch_filter(punch_ins, dur)
        if pf:
            vf.append(pf)
    if subs_path and subs_path.exists():
        # Burned-in karaoke captions, styled for a phone held at arm's length:
        # heavy weight, thick black outline, sat above the bottom safe area so
        # platform UI (username, sound, buttons) doesn't cover them.
        esc = str(subs_path).replace("\\", "/").replace(":", "\\:")
        style = ("FontName=Arial Black,Fontsize=17,Bold=1,PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00000000,BorderStyle=1,Outline=4,Shadow=1,"
                 "Alignment=2,MarginV=320")
        vf.append(f"subtitles='{esc}':force_style='{style}'")

    args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            *_seek(source, start_s, dur), "-vf", ",".join(vf),
            "-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", *_audio_args(), str(out)]
    _run(args)
    return out


def keyframes(source: Path, out_dir: Path, start_s: float, end_s: float,
              every_s: float = 4.0, cap: int = 8) -> list[Path]:
    """Keyframes for visual QC: first, last, and one every ~4s in between."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = max(0.2, end_s - start_s)
    times = [0.05]
    t = every_s
    while t < dur - 0.2 and len(times) < cap - 1:
        times.append(t)
        t += every_s
    times.append(max(0.1, dur - 0.2))

    made = []
    for i, rel in enumerate(times):
        dest = out_dir / f"kf{i:02d}.jpg"
        args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start_s + rel:.3f}", "-i", str(source),
                "-frames:v", "1", "-q:v", "4", "-vf", "scale=640:-2", str(dest)]
        try:
            _run(args, timeout=120)
            if dest.exists():
                made.append(dest)
        except (RuntimeError, subprocess.SubprocessError):
            continue
    return made


def write_karaoke_srt(words: list[dict], dest: Path, group: int = 4) -> Optional[Path]:
    """Word-timed captions in small groups, styled bold/high-contrast by the ASS
    override in the subtitles filter. SRT keeps it portable."""
    if not words:
        return None

    def ts(sec: float) -> str:
        ms = max(0, int(round(sec * 1000)))
        return f"{ms // 3600000:02d}:{(ms % 3600000) // 60000:02d}:{(ms % 60000) // 1000:02d},{ms % 1000:03d}"

    lines, idx = [], 1
    for i in range(0, len(words), group):
        chunk = [w for w in words[i:i + group] if w.get("w")]
        if not chunk:
            continue
        text = " ".join(w["w"] for w in chunk).strip().upper()
        lines += [str(idx), f"{ts(chunk[0]['start'])} --> {ts(chunk[-1]['end'])}", text, ""]
        idx += 1
    if idx == 1:
        return None
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest
