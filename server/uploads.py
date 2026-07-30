"""Chunked, resumable uploads.

Why not a plain multipart POST: a 2 GB file over LTE will drop, and a plain POST
restarts from zero. The phone slices the file and sends fixed-size chunks; each
lands as its own .part file. On reconnect the phone asks which indexes we already
have and sends only the gaps, so a dropped connection costs one chunk, not the
whole upload.

The upload id is derived from (name, size, mtime), so closing the browser tab and
re-picking the same file resumes instead of starting over.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from . import config

CHUNK_DIRNAME = "chunks"
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _staging(upload_id: str) -> Path:
    return config.TMP / CHUNK_DIRNAME / upload_id


def upload_id_for(name: str, size: int, mtime: float) -> str:
    raw = f"{name}|{size}|{int(mtime)}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:20]


def safe_filename(name: str) -> str:
    base = Path(str(name or "video")).name
    cleaned = SAFE_NAME.sub("_", base).strip("._") or "video"
    return cleaned[:120]


def init(name: str, size: int, mtime: float, chunk_size: int) -> dict:
    uid = upload_id_for(name, size, mtime)
    d = _staging(uid)
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "upload_id": uid,
        "name": name,
        "safe_name": safe_filename(name),
        "size": int(size),
        "mtime": float(mtime),
        "chunk_size": int(chunk_size),
        "total_chunks": max(1, -(-int(size) // int(chunk_size))),
        "created_at": time.time(),
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {**meta, "received": received(uid)}


def meta(upload_id: str) -> Optional[dict]:
    p = _staging(upload_id) / "meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def received(upload_id: str) -> list[int]:
    d = _staging(upload_id)
    if not d.exists():
        return []
    out = []
    for p in d.glob("*.part"):
        try:
            out.append(int(p.stem))
        except ValueError:
            continue
    return sorted(out)


def save_chunk(upload_id: str, index: int, body: bytes) -> dict:
    d = _staging(upload_id)
    if not d.exists():
        raise FileNotFoundError("unknown upload id")
    # Write to a temp name then rename: a chunk killed mid-write must not look
    # complete, or assembly would splice a truncated slice into the video.
    tmp = d / f"{index}.part.tmp"
    tmp.write_bytes(body)
    tmp.replace(d / f"{index}.part")
    got = received(upload_id)
    m = meta(upload_id) or {}
    return {"received_count": len(got), "total_chunks": m.get("total_chunks", 0)}


def missing(upload_id: str) -> list[int]:
    m = meta(upload_id)
    if not m:
        return []
    have = set(received(upload_id))
    return [i for i in range(m["total_chunks"]) if i not in have]


def assemble(upload_id: str) -> Path:
    m = meta(upload_id)
    if not m:
        raise FileNotFoundError("unknown upload id")
    gaps = missing(upload_id)
    if gaps:
        raise ValueError(f"upload incomplete, missing {len(gaps)} chunk(s): {gaps[:8]}")

    d = _staging(upload_id)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = config.UPLOADS / f"{stamp}_{m['safe_name']}"
    if dest.suffix.lower() not in config.VIDEO_SUFFIXES:
        dest = dest.with_suffix(dest.suffix + ".mp4") if dest.suffix else dest.with_suffix(".mp4")

    with dest.open("wb") as out:
        for i in range(m["total_chunks"]):
            part = d / f"{i}.part"
            with part.open("rb") as fh:
                shutil.copyfileobj(fh, out, length=1024 * 1024)

    actual = dest.stat().st_size
    if m["size"] and actual != m["size"]:
        dest.unlink(missing_ok=True)
        raise ValueError(f"assembled size {actual} != declared {m['size']}")

    shutil.rmtree(d, ignore_errors=True)
    return dest


def abandon(upload_id: str) -> None:
    shutil.rmtree(_staging(upload_id), ignore_errors=True)


def sweep(max_age_h: float = 48.0) -> int:
    """Drop staging dirs from uploads that were never finished."""
    base = config.TMP / CHUNK_DIRNAME
    if not base.exists():
        return 0
    cutoff = time.time() - max_age_h * 3600
    n = 0
    for d in base.iterdir():
        if not d.is_dir():
            continue
        m = meta(d.name)
        created = (m or {}).get("created_at", 0)
        if created and created < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n
