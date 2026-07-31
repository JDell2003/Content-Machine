"""Neural speech enhancement on the 3060.

Why this exists: the ffmpeg chain (highpass -> RNNoise -> EQ -> compressor ->
loudnorm) cleans and levels audio, but it can't *reconstruct* speech. On a room
recording with a phone mic there's a ceiling. A trained enhancement model rebuilds
the speech itself.

What actually installed on Windows:
  * DeepFilterNet   - NO. deepfilterlib needs a Rust toolchain, no py3.12 wheel.
  * resemble-enhance - NO. requires deepspeed, which does not build on Windows.
  * speechbrain     - YES. MetricGAN+ runs on CUDA and installs clean.

Honest limitation: MetricGAN+ works at 16 kHz. That's the speech band, so it
sounds clearer, but the very top end is gone versus the 48 kHz source. On noisy
room audio that's a clear win; on already-clean audio it can sound duller. Hence
the toggle - compare on your own footage rather than take my word.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "data" / "models" / "speechbrain"
_MODEL = None


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def available() -> bool:
    try:
        import speechbrain  # noqa: F401,PLC0415
        import torch  # noqa: F401,PLC0415
        return True
    except ImportError:
        return False


def _load():
    """Load once and keep it — model init is slower than the inference."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    import torch  # noqa: PLC0415
    from speechbrain.inference.enhancement import SpectralMaskEnhancement  # noqa: PLC0415
    device = "cuda" if torch.cuda.is_available() else "cpu"
    # COPY, not SYMLINK: speechbrain defaults to symlinking model files, which
    # raises WinError 1314 unless Windows is in Developer Mode or running as
    # admin. Copying costs a few MB and always works.
    kwargs = {"source": "speechbrain/metricgan-plus-voicebank",
              "savedir": str(MODEL_DIR / "metricgan"),
              "run_opts": {"device": device}}
    try:
        from speechbrain.utils.fetching import LocalStrategy  # noqa: PLC0415
        kwargs["local_strategy"] = LocalStrategy.COPY
    except ImportError:
        pass
    _MODEL = SpectralMaskEnhancement.from_hparams(**kwargs)
    return _MODEL


def enhance_file(src: Path, dest_wav: Path) -> Optional[Path]:
    """Extract audio, enhance it, write a 16 kHz wav. Returns None on any failure
    so the caller falls back to the ffmpeg chain rather than losing the clip."""
    if not available():
        return None
    try:
        import torch  # noqa: PLC0415
        import torchaudio  # noqa: PLC0415

        # The model wants 16 kHz mono; let ffmpeg do the resample.
        raw = dest_wav.with_name(dest_wav.stem + "_raw.wav")
        subprocess.run(
            [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(raw)],
            check=True, capture_output=True, timeout=600)

        model = _load()
        noisy = model.load_audio(str(raw)).unsqueeze(0)
        lengths = torch.tensor([1.0])
        enhanced = model.enhance_batch(noisy, lengths=lengths)
        torchaudio.save(str(dest_wav), enhanced.cpu(), 16000)
        raw.unlink(missing_ok=True)
        return dest_wav if dest_wav.exists() else None
    except Exception:  # noqa: BLE001 - enhancement is a bonus, never a blocker
        return None


def mux_args(enhanced_wav: Path) -> list[str]:
    """ffmpeg args to use the enhanced track instead of the source audio."""
    return ["-i", str(enhanced_wav), "-map", "0:v:0", "-map", "1:a:0"]
