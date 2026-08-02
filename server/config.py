"""Paths and settings. Everything heavy lives on D: — C: has very little free space,
so model caches and temp files must never default to the user profile."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
JOBS = DATA / "jobs"
CLIPS = DATA / "clips"
APPROVED = DATA / "approved"
LOGS = DATA / "logs"
MODELS = DATA / "models"
TMP = DATA / "tmp"
VENDOR = ROOT / "vendor"

for _d in (UPLOADS, JOBS, CLIPS, APPROVED, LOGS, MODELS, TMP, VENDOR):
    _d.mkdir(parents=True, exist_ok=True)

# Keep HuggingFace / torch model downloads off C:.
os.environ.setdefault("HF_HOME", str(MODELS / "hf"))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(MODELS / "hf" / "hub"))
os.environ.setdefault("TORCH_HOME", str(MODELS / "torch"))
os.environ.setdefault("XDG_CACHE_HOME", str(MODELS / "cache"))


def _s(key: str, default: str = "") -> str:
    return str(os.environ.get(key, default) or default).strip()


def _i(key: str, default: int) -> int:
    try:
        return int(float(_s(key, str(default))))
    except (TypeError, ValueError):
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(_s(key, str(default)))
    except (TypeError, ValueError):
        return default


def _b(key: str, default: bool) -> bool:
    return _s(key, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


PIN = _s("CM_PIN", "000000")
SESSION_SECRET = _s("CM_SESSION_SECRET", "dev-insecure-secret")
HOST = _s("CM_HOST", "0.0.0.0")
PORT = _i("CM_PORT", 3000)

BRAIN_MODE = _s("CM_BRAIN", "auto").lower()          # auto | cli | api
ANTHROPIC_API_KEY = _s("ANTHROPIC_API_KEY")
RANK_MODEL = _s("CM_RANK_MODEL", "opus")
CAPTION_MODEL = _s("CM_CAPTION_MODEL", "sonnet")
CHAT_MODEL = _s("CM_CHAT_MODEL", "sonnet")
BRAIN_TIMEOUT_S = _i("CM_BRAIN_TIMEOUT_S", 900)

WHISPER_MODEL = _s("CM_WHISPER_MODEL", "medium")
WHISPER_DEVICE = _s("CM_WHISPER_DEVICE", "auto")
WHISPER_COMPUTE = _s("CM_WHISPER_COMPUTE", "auto")

CLIP_MIN_S = _i("CM_CLIP_MIN_S", 30)
CLIP_MAX_S = _i("CM_CLIP_MAX_S", 80)
MAKE_VERTICAL = _b("CM_MAKE_VERTICAL", True)
CUT_SILENCE = _b("CM_CUT_SILENCE", False)
# Captions are NOT burned at render time by default. The transcript is written
# beside the clip either way, so the editor can overlay it live and burn the
# final wording/size/colour in when you approve. Burning during the batch means
# every restyle needs a re-render, and it bakes text into clips you may reject.
BURN_CAPTIONS_AT_RENDER = _b("CM_BURN_CAPTIONS_AT_RENDER", False)

# Transition into the CTA outro. Without these the join is a hard cut, which
# reads as a glitch rather than a deliberate hand-off. Both video and audio
# fade, so the voice does not slam into the CTA either.
OUTRO_FADE_OUT = _f("CM_OUTRO_FADE_OUT", 0.5)   # end of the clip
OUTRO_FADE_IN = _f("CM_OUTRO_FADE_IN", 0.5)     # start of the outro

# Look and sound. Presets are per environment because the fix differs — a warm
# flat office and a green-lit gym need opposite corrections. Both scale with an
# intensity dial so a wrong guess is adjustable instead of a ruined clip.
COLOR_PRESET = _s("CM_COLOR_PRESET", "office")      # office|gym|outdoor|neutral
COLOR_INTENSITY = _f("CM_COLOR_INTENSITY", 1.0)     # 0 = off
AUDIO_PRESET = _s("CM_AUDIO_PRESET", "office")      # office|gym|quiet|raw
AUDIO_INTENSITY = _f("CM_AUDIO_INTENSITY", 1.0)     # 0 = level only

FRAMEWORKS_PATH = ROOT / "FRAMEWORKS.md"

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".mts", ".wmv"}
