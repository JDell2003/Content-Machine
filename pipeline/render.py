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

# Encoder settings.
#
# The first version used crf 16 / preset slow - archival quality - on a 4K HEVC
# source. That ran ~12x SLOWER than realtime (935s for an 80s clip), which put a
# full video at roughly overnight. It was also pointless: the deliverable is a
# 1080p clip that the platform re-compresses on upload, so archival settings buy
# nothing visible.
#
# NVENC on the 3060 does the encode in dedicated silicon instead of on the CPU,
# and CUDA handles the 4K decode. CPU x264 stays as the fallback.
# Measured on the real 4K source (80s clip): medium 50s, fast 43s,
# veryfast 33s. The deliverable is a 1080p clip the platform re-compresses
# on upload, so "fast" is the sweet spot - 14% quicker than medium with no
# visible difference on talking-head footage.
CRF = "18"
PRESET = "fast"

# NVENC: constant-quality mode. cq 21 at p5 is visually equivalent to x264 crf 18
# for talking-head footage and runs an order of magnitude faster.
NVENC_CQ = "21"
NVENC_PRESET = "p5"

_NVENC_OK: Optional[bool] = None


def nvenc_available() -> bool:
    """Probe once: encode two black frames and see if NVENC accepts them.
    Cheaper than discovering at render time that the driver refuses."""
    global _NVENC_OK
    if _NVENC_OK is not None:
        return _NVENC_OK
    try:
        p = subprocess.run(
            [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=black:s=256x256:d=0.1", "-c:v", "h264_nvenc",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120)
        _NVENC_OK = p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        _NVENC_OK = False
    return _NVENC_OK


def _video_codec_args() -> list[str]:
    if nvenc_available():
        return ["-c:v", "h264_nvenc", "-preset", NVENC_PRESET, "-rc", "vbr",
                "-cq", NVENC_CQ, "-b:v", "0", "-profile:v", "high",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-pix_fmt", "yuv420p"]


def _decode_args(source: Path) -> list[str]:
    """No hardware decode. MEASURED: adding -hwaccel cuda broke the input seek on
    this HEVC source - ffmpeg decoded from the start of the file for every clip,
    taking 11 MINUTES per clip instead of seconds. The filter chain is CPU-side
    anyway, so hw decode only bought GPU->CPU frame copies. Encode still uses
    NVENC; that's where the win actually is."""
    return []

YUNET_MODEL = Path(__file__).resolve().parent.parent / "vendor" / "models" / "face_detection_yunet_2023mar.onnx"


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


_DIMS: dict[str, tuple[int, int]] = {}


def decoded_dims(source: Path) -> tuple[int, int]:
    """Dimensions as OpenCV actually decodes them.

    These are NOT always the probe dimensions. iPhone video is stored portrait
    with a rotation matrix; ffprobe reports the display size (3840x2160) while
    OpenCV hands back the stored frame (2160x3840) with no rotation applied.
    Face coordinates come from OpenCV, so every crop must be computed against
    THIS, or the box lands somewhere else entirely.
    """
    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        return probe_dims(source)
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return probe_dims(source)
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
        return (w, h) if w and h else probe_dims(source)
    finally:
        cap.release()


def probe_dims(source: Path) -> tuple[int, int]:
    """Source width/height as ffmpeg will present them to the filter chain."""
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
    """Fast input seek only. -ss BEFORE -i jumps by keyframe index instead of
    decoding forward, which is the whole game on a 10 GB file."""
    return ["-ss", f"{start_s:.3f}", "-i", str(source), "-t", f"{dur_s:.3f}"]


def cut_segment(source: Path, dest: Path, start_s: float, dur_s: float) -> Path:
    """Lossless extract of just this clip's span.

    -c copy is a REMUX, not a re-encode: the video bitstream is untouched, so the
    one-encode-from-source rule holds. Everything downstream (face detection,
    silence scan, loudness measure, both renders) then works on an ~80s file
    instead of seeking around a 67-minute 4K one. This is the single biggest
    speed win in the pipeline.
    """
    args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, start_s - 1.0):.3f}", "-i", str(source),
            "-t", f"{dur_s + 2.0:.3f}", "-c", "copy", "-avoid_negative_ts", "1",
            str(dest)]
    _run(args, timeout=600)
    return dest


def _audio_args() -> list[str]:
    # Re-encoding audio at 192k AAC is inaudible next to a re-cut video and
    # sidesteps container/timestamp problems that -c:a copy causes on trims.
    return ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]


def subtitle_filter(subs_path: Path, vertical: bool,
                    style_override: Optional[dict] = None) -> str:
    """Burned-in karaoke captions. Styled for a phone at arm's length: heavy
    weight, thick outline so they read over any background, sat above the safe
    area so platform UI doesn't cover them.

    Size/position/outline come from data/caption-settings.json so they can be
    changed from the Review page without touching code.
    """
    from . import captions as _cap  # noqa: PLC0415 - avoids a circular import
    esc = str(subs_path).replace("\\", "/").replace(":", r"\:")
    return f"subtitles='{esc}':force_style='{_cap.force_style(vertical, style_override)}'"


def _keep_filters(keeps: list[tuple[float, float]], dur: float) -> tuple[str, str]:
    """Jump-cut filters that drop dead air in the SAME pass as everything else.

    select/aselect keep only the speech ranges, then setpts/asetpts restamp so the
    output has no gaps. Doing it here rather than as a pre-trim keeps the promise
    of one encode from source.
    """
    if not keeps or (len(keeps) == 1 and keeps[0][1] - keeps[0][0] >= dur - 0.05):
        return "", ""
    gate = "+".join(f"between(t\\,{a:.3f}\\,{b:.3f})" for a, b in keeps)
    return f"select='{gate}',setpts=N/FRAME_RATE/TB", f"aselect='{gate}',asetpts=N/SR/TB"


# Longest edge of a delivered clip. Every platform this feeds caps at 1080x1920
# (Reels, TikTok, Shorts) or 1920x1080 (YouTube landscape), so rendering the
# native 4K costs 4x the pixels through every filter and the encoder and not one
# viewer ever sees the difference.
#
# MEASURED on a 30s 4K portrait clip: 75.3s at native 4K vs 30.5s capped
# = 2.5x faster, output 1080x1920. Not the 4x you'd predict from pixel count,
# because decoding the 4K HEVC source costs the same either way — and GPU decode
# did NOT help (31.9s, slightly worse than CPU).
MAX_DELIVERY_EDGE = 1920


def _delivery_scale(source: Path) -> str:
    """Cap the long edge, preserving aspect and orientation. Never upscales.

    A square bounding box is the trick: fitting inside 1920x1920 with
    force_original_aspect_ratio=decrease turns 2160x3840 into 1080x1920 and
    3840x2160 into 1920x1080, with no conditional expressions. That matters —
    commas inside ffmpeg if() calls have broken this filter chain before.
    """
    w, h = probe_dims(source)
    cap = MAX_DELIVERY_EDGE
    # probe_dims reports pre-rotation dimensions on iPhone footage, but the long
    # edge is the same either way, so this test is orientation-proof.
    if not w or not h or max(w, h) <= cap:
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    return (f"scale={cap}:{cap}:force_original_aspect_ratio=decrease"
            f":force_divisible_by=2:flags=bicubic")


def render_wide(source: Path, out: Path, start_s: float, end_s: float,
                punch_ins: Optional[list[dict]] = None,
                keeps: Optional[list[tuple[float, float]]] = None,
                afilter: str = "", subs_path: Optional[Path] = None,
                grade: str = "", style_override: Optional[dict] = None) -> Path:
    """16:9 at source resolution, one encode from the original file."""
    dur = max(0.2, end_s - start_s)
    vf: list[str] = []
    vsel, asel = _keep_filters(keeps or [], dur)
    if vsel:
        vf.append(vsel)
    if punch_ins:
        pf = _punch_filter(punch_ins, dur)
        if pf:
            vf.append(pf)
    vf.append(_delivery_scale(source))
    # Grade BEFORE captions: the caption is already the colour we chose, and
    # running contrast/saturation over it would shift it and crunch the edges.
    if grade:
        vf.append(grade)
    # Captions go LAST so they're burned on at final scale and stay crisp.
    if subs_path and subs_path.exists():
        vf.append(subtitle_filter(subs_path, vertical=False, style_override=style_override))

    af = ",".join(x for x in (asel, afilter) if x)
    args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *_decode_args(source),
            *_seek(source, start_s, dur), "-vf", ",".join(vf)]
    if af:
        args += ["-af", af]
    args += [*_video_codec_args(), "-movflags", "+faststart", *_audio_args(), str(out)]
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
                 noise_db: int = -38, min_silence_s: float = 1.2,
                 keep_pad_s: float = 0.25,
                 max_cuts_per_min: float = 4.0) -> list[tuple[float, float]]:
    """Find the KEEP ranges (clip-relative) after removing dead air.

    TUNED AFTER REAL FEEDBACK. The first version used -32dB / 0.45s / 0.12s pad
    and produced 16 keep-ranges in an 80-second clip - a cut every 5 seconds,
    which chopped between words and mid-sentence. It sounded broken.

    A person talking naturally pauses 0.3-0.8s between sentences and breathes
    mid-thought. Those are NOT dead air. Only a gap over ~1.2s is worth removing,
    the threshold sits lower (-38dB) so soft speech and room tone don't register
    as silence, and the pad is wide enough not to clip consonants.

    Final guard: if the result still wants more than ~4 cuts a minute, the
    detection is wrong for this audio and we return one uncut range instead.
    Choppy is worse than long.
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
    if not keeps:
        return [(0.0, dur)]

    # Sanity guard: too many cuts means we're slicing speech, not silence.
    cuts = len(keeps) - 1
    if cuts > max(1.0, (dur / 60.0) * max_cuts_per_min):
        return [(0.0, dur)]
    # Never drop more than a quarter of the clip - that's not dead air any more.
    if sum(b - a for a, b in keeps) < dur * 0.75:
        return [(0.0, dur)]
    return keeps


def face_track(source: Path, start_s: float, end_s: float, *, samples: int = 24) -> list[dict]:
    """Sample frames and find where the faces are. Analysis only — writes nothing.
    Returns [{t_rel, cx_frac}] centre-x as a fraction of width."""
    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        return []

    # YuNet (DNN) rather than a Haar cascade. Haar was returning confident false
    # positives on wide 4K room shots - it framed a painting on the wall as a
    # face - which made auto-reframing worse than no reframing at all. YuNet is a
    # 227 KB ONNX model from opencv/opencv_zoo and is dramatically more reliable
    # on this footage.
    detector_cls = getattr(cv2, "FaceDetectorYN", None)
    if detector_cls is None or not YUNET_MODEL.exists():
        return []

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return []
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1
        # Detect on a downscaled frame: 4K costs a lot per frame and buys nothing
        # for face location. Coordinates get scaled back up afterwards.
        det_w = 960
        det_h = max(2, int(round(h * det_w / max(1, w))))
        scale = w / float(det_w)
        detector = detector_cls.create(
            str(YUNET_MODEL), "", (det_w, det_h),
            score_threshold=0.75,  # strict: a wrong face is worse than no face
            nms_threshold=0.3, top_k=20)

        dur = max(0.2, end_s - start_s)
        out = []
        for i in range(samples):
            t = start_s + dur * (i / max(1, samples - 1))
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            small = cv2.resize(frame, (det_w, det_h))
            _, dets = detector.detect(small)
            if dets is None or len(dets) == 0:
                continue
            # YuNet rows: x, y, w, h, 5 landmarks..., score
            faces = [(float(d[0]) * scale, float(d[1]) * scale,
                      float(d[2]) * scale, float(d[3]) * scale) for d in dets]
            # Midpoint between the outermost faces keeps both people in frame when
            # two are talking, instead of snapping to whoever detects strongest.
            lo = min(x for x, _, _, _ in faces)
            hi = max(x + fw for x, _, fw, _ in faces)
            # The midpoint between two faces is the WALL BETWEEN THEM. A 9:16 crop
            # of a wide shot is far too narrow to hold both people, so centring on
            # the midpoint framed the painting between them. Track the biggest
            # face (nearest / the one on camera) and let the caller decide whether
            # both actually fit.
            biggest = max(faces, key=lambda f: f[3])
            solo_cx = (biggest[0] + biggest[2] / 2.0)
            # Vertical position and face size matter as much as x: without them the
            # 9:16 crop keeps full height and fills the frame with ceiling.
            eye_y = min(y + fh * 0.42 for _, y, _, fh in faces)
            big = max(fh for _, _, _, fh in faces)
            out.append({"t_rel": round(dur * (i / max(1, samples - 1)), 2),
                        "cx_frac": round(((lo + hi) / 2.0) / w, 4),
                        "solo_cx_frac": round(solo_cx / w, 4),
                        "span_frac": round((hi - lo) / w, 4),
                        "eye_frac": round((biggest[1] + biggest[3] * 0.42) / max(1, h), 4),
                        "face_frac": round(big / max(1, h), 4),
                        "faces": int(len(faces))})
        return out
    finally:
        cap.release()


def render_vertical(source: Path, out: Path, start_s: float, end_s: float, *,
                    track: Optional[list[dict]] = None,
                    punch_ins: Optional[list[dict]] = None,
                    subs_path: Optional[Path] = None,
                    keeps: Optional[list[tuple[float, float]]] = None,
                    afilter: str = "") -> Optional[Path]:
    """9:16 from source in one pass. Crop x follows the face track. Returns None
    when no faces were found anywhere (no point guessing a crop)."""
    dur = max(0.2, end_s - start_s)
    track = track if track is not None else face_track(source, start_s, end_s)
    if not track:
        return None

    # Do both faces fit in a 9:16 slice? If not, follow the nearest speaker
    # instead of centring on empty wall between them. Uses DECODED dims because
    # the face fractions came from OpenCV.
    sw0, sh0 = decoded_dims(source)
    crop_frac = (sh0 * 9 / 16) / max(1, sw0) if sw0 and sh0 else 0.3
    spans = [p.get("span_frac", 0.0) for p in track]
    both_fit = (sum(spans) / len(spans)) < crop_frac * 0.9
    key = "cx_frac" if both_fit else "solo_cx_frac"

    # Smooth the path so the frame drifts instead of snapping shot-to-shot.
    smoothed: list[tuple[float, float]] = []
    prev = track[0][key]
    for p in track:
        prev = prev + 0.35 * (p.get(key, p["cx_frac"]) - prev)
        smoothed.append((p["t_rel"], prev))

    # Crop box computed numerically from the real source dimensions rather than
    # with min()/max() in the filter string. ffmpeg splits filter arguments on
    # commas, so every comma inside a function call has to be escaped as "\,";
    # doing the arithmetic here keeps the expression far simpler and this bug
    # (No such filter: 'ih*9/16):min(ih') from recurring.
    # ffmpeg's filter chain sees the ROTATED (display) frame, so the crop box is
    # built in probe dimensions - while the face fractions above came from
    # OpenCV's unrotated frame. When the two disagree the axes are swapped, so
    # the fractions must be swapped with them.
    sw, sh = probe_dims(source)
    if not sw or not sh:
        return None
    dw, dh = decoded_dims(source)
    rotated = bool(dw and dh and (dw > dh) != (sw > sh))

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

    # On rotated footage OpenCV's x-fraction is ffmpeg's y-fraction and vice
    # versa. Without this swap the crop tracks the wrong axis entirely.
    if rotated:
        smoothed = [(t, e) for (t, _), e in zip(smoothed, [p.get("eye_frac", 0.35) for p in track])]
        eye_source = [p.get(key, p["cx_frac"]) for p in track]
    else:
        eye_source = [p.get("eye_frac", 0.35) for p in track]

    eye_avg = sum(eye_source) / len(eye_source)  # hold y steady; a bobbing frame is worse
    parts = []
    for i, (t, cx) in enumerate(smoothed):
        nxt = smoothed[i + 1][0] if i + 1 < len(smoothed) else dur + 1
        parts.append((t, nxt, *box_for(cx, eye_avg)))
    x_expr, y_fixed = str(parts[-1][2]), parts[-1][3]
    for _t, nxt, x, _y in reversed(parts[:-1]):
        x_expr = f"if(lt(t\\,{nxt:.2f})\\,{x}\\,{x_expr})"

    # Crop then scale to a standard 1080x1920 so every clip is the same shape.
    # Silence cuts go FIRST: the crop path is per-frame, and dropping frames
    # afterwards would leave the pan expression referencing the wrong times.
    vf = []
    vsel, asel = _keep_filters(keeps or [], dur)
    if vsel:
        vf.append(vsel)
    vf += [f"crop={crop_w}:{crop_h}:{x_expr}:{y_fixed}", "scale=1080:1920:flags=lanczos"]
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

    af = ",".join(x for x in (asel, afilter) if x)
    args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *_decode_args(source),
            *_seek(source, start_s, dur), "-vf", ",".join(vf)]
    if af:
        args += ["-af", af]
    args += [*_video_codec_args(), "-movflags", "+faststart", *_audio_args(), str(out)]
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


def write_karaoke_srt(words: list[dict], dest: Path,
                      group: Optional[int] = None) -> Optional[Path]:
    """Word-timed captions in small groups, styled bold/high-contrast by the ASS
    override in the subtitles filter. SRT keeps it portable.

    Spelling corrections are applied HERE, so a fix entered once ("etavis" ->
    "Etavis") lands on every clip of every future video, not just this one.
    """
    from . import captions as _cap  # noqa: PLC0415
    if not words:
        return None
    settings = _cap.load()
    corrections = settings["corrections"]
    group = int(group or settings["group"])

    def ts(sec: float) -> str:
        ms = max(0, int(round(sec * 1000)))
        return f"{ms // 3600000:02d}:{(ms % 3600000) // 60000:02d}:{(ms % 60000) // 1000:02d},{ms % 1000:03d}"

    lines, idx = [], 1
    for i in range(0, len(words), group):
        chunk = [w for w in words[i:i + group] if w.get("w")]
        if not chunk:
            continue
        text = " ".join(w["w"] for w in chunk).strip()
        # Correct BEFORE uppercasing: the dictionary holds real capitalisation
        # ("Etavis"), which an earlier .upper() would have destroyed.
        text = _cap.apply_corrections(text, corrections)
        if settings["uppercase"]:
            text = text.upper()
        lines += [str(idx), f"{ts(chunk[0]['start'])} --> {ts(chunk[-1]['end'])}", text, ""]
        idx += 1
    if idx == 1:
        return None
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest
