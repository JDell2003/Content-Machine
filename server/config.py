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
BRAIN_TIMEOUT_S = _i("CM_BRAIN_TIMEOUT_S", 900)

WHISPER_MODEL = _s("CM_WHISPER_MODEL", "medium")
WHISPER_DEVICE = _s("CM_WHISPER_DEVICE", "auto")
WHISPER_COMPUTE = _s("CM_WHISPER_COMPUTE", "auto")

CLIP_MIN_S = _i("CM_CLIP_MIN_S", 30)
CLIP_MAX_S = _i("CM_CLIP_MAX_S", 80)
MAKE_VERTICAL = _b("CM_MAKE_VERTICAL", True)

FRAMEWORKS_PATH = ROOT / "FRAMEWORKS.md"

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".mts", ".wmv"}
