"""Transcription via faster-whisper.

GPU first (proven: cuda/float16 on an RTX 3060, ~5.7x realtime), CPU int8 as the
fallback. Word timestamps are always on — they cost little and Upgrade Pass 2's
karaoke captions and punch-in placement both need them, so capturing them once
here avoids a second pass later.

Transcripts are cached next to the source: re-running ranking with a tuned prompt
must not re-transcribe an hour of audio.
"""
from __future__ import annotations

import json
import os
import site
from pathlib import Path
from typing import Callable, Optional

_DLLS_READY = False


def _prepare_cuda_dlls() -> None:
    """CTranslate2 needs cuBLAS/cuDNN from the pip nvidia-* wheels; on Windows
    those DLL dirs are not on PATH automatically."""
    global _DLLS_READY
    if _DLLS_READY:
        return
    for sp in site.getsitepackages():
        for sub in ("nvidia/cudnn/bin", "nvidia/cublas/bin"):
            d = Path(sp) / sub
            if d.is_dir():
                try:
                    os.add_dll_directory(str(d))
                except (OSError, AttributeError):
                    pass
                os.environ["PATH"] = f"{d};{os.environ.get('PATH', '')}"
    _DLLS_READY = True


def pick_device(requested: str = "auto", compute: str = "auto") -> tuple[str, str]:
    if requested != "auto":
        return requested, (compute if compute != "auto" else ("float16" if requested == "cuda" else "int8"))
    try:
        import ctranslate2  # noqa: PLC0415
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", (compute if compute != "auto" else "float16")
    except Exception:  # noqa: BLE001 - any import/driver problem means CPU
        pass
    return "cpu", (compute if compute != "auto" else "int8")


def cache_path(source: Path, model: str) -> Path:
    return source.parent / f"{source.stem}.{model}.transcript.json"


def transcribe(source: Path, *, model_size: str = "medium", device: str = "auto",
               compute: str = "auto", models_dir: Optional[Path] = None,
               report: Optional[Callable[..., None]] = None) -> dict:
    """-> {duration, language, device, segments:[{start,end,text,words:[{w,start,end}]}]}"""
    cache = cache_path(source, model_size)
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("segments"):
                if report:
                    report(message=f"Reusing cached transcript ({len(data['segments'])} segments)")
                return data
        except (OSError, json.JSONDecodeError):
            pass

    _prepare_cuda_dlls()
    from faster_whisper import WhisperModel  # noqa: PLC0415

    dev, comp = pick_device(device, compute)
    if report:
        report(message=f"Loading whisper-{model_size} on {dev}/{comp}")

    def _load(d, c):
        return WhisperModel(model_size, device=d, compute_type=c,
                            download_root=str(models_dir) if models_dir else None)

    try:
        mdl = _load(dev, comp)
    except Exception as exc:  # noqa: BLE001
        if dev == "cuda":
            if report:
                report(message=f"GPU load failed ({str(exc)[:80]}), falling back to CPU")
            dev, comp = "cpu", "int8"
            mdl = _load(dev, comp)
        else:
            raise

    segs_iter, info = mdl.transcribe(
        str(source), beam_size=5, vad_filter=True, word_timestamps=True,
        # 400ms keeps natural pauses intact; shorter shreds sentences mid-breath.
        vad_parameters={"min_silence_duration_ms": 400},
    )

    total = float(getattr(info, "duration", 0) or 0)
    segments = []
    for s in segs_iter:
        words = [{"w": w.word, "start": round(w.start, 3), "end": round(w.end, 3)}
                 for w in (s.words or [])]
        segments.append({"start": round(s.start, 3), "end": round(s.end, 3),
                         "text": s.text.strip(), "words": words})
        if report and total and len(segments) % 25 == 0:
            report(progress_hint=min(0.99, s.end / total),
                   message=f"Transcribing… {s.end / 60:.1f}/{total / 60:.1f} min")

    out = {"duration": total, "language": getattr(info, "language", "en"),
           "device": f"{dev}/{comp}", "model": model_size, "segments": segments}
    try:
        cache.write_text(json.dumps(out), encoding="utf-8")
    except OSError:
        pass
    if report:
        report(message=f"Transcribed {len(segments)} segments on {dev}/{comp}")
    return out
