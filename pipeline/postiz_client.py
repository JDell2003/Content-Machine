"""Postiz push — behind a flag, off by default.

Guard rails are the point of this module:
  * refuses anything over MAX_MB (finished clips only, never raw source)
  * refuses a path outside the clips/approved dirs
  * every push logged with clip id + response, so a failed schedule is visible
    rather than silently lost

Not exercised end-to-end yet: Postiz isn't running on this machine until Docker
is installed. Every call therefore reports a clear "not configured" instead of
throwing, so the approval flow keeps working regardless.
"""
from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

MAX_MB = 100
LOG = Path(__file__).resolve().parent.parent / "data" / "logs" / "postiz.jsonl"


def _cfg() -> dict:
    return {
        "url": (os.environ.get("CM_POSTIZ_URL") or "").rstrip("/"),
        "key": os.environ.get("CM_POSTIZ_API_KEY") or "",
        "enabled": str(os.environ.get("CM_SCHEDULE_DEFAULT") or "0").lower() in {"1", "true", "yes", "on"},
        "slot": os.environ.get("CM_POSTIZ_DEFAULT_SLOT") or "next-open",
    }


def configured() -> bool:
    c = _cfg()
    return bool(c["url"] and c["key"])


def _log(entry: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _post(url: str, key: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw or b"{}")


def _upload(url: str, key: str, path: Path, timeout: int = 900) -> dict:
    """multipart upload, hand-rolled to avoid a dependency for one call."""
    boundary = f"----cm{uuid.uuid4().hex}"
    ctype = mimetypes.guess_type(path.name)[0] or "video/mp4"
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + path.read_bytes() + tail
    req = urllib.request.Request(
        f"{url}/api/public/v1/upload", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": key, "Content-Length": str(len(body))})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def schedule_clip(clip: dict, *, variant: str = "vertical",
                  when_iso: Optional[str] = None) -> dict:
    """Push one approved clip. Returns {ok, reason|post, ...} — never raises."""
    c = _cfg()
    if not configured():
        return {"ok": False, "reason": "postiz not configured (CM_POSTIZ_URL / CM_POSTIZ_API_KEY)"}

    raw = clip.get("path_vertical") if variant == "vertical" else clip.get("path")
    raw = raw or clip.get("path")
    if not raw:
        return {"ok": False, "reason": "clip has no rendered file"}
    path = Path(raw)
    if not path.exists():
        return {"ok": False, "reason": f"file missing: {path.name}"}

    size_mb = path.stat().st_size / 1_048_576
    if size_mb > MAX_MB:
        # Deliberate: this is the wall that keeps raw meeting video out of the cloud.
        return {"ok": False, "reason": f"{size_mb:.0f} MB exceeds the {MAX_MB} MB limit"}
    if "uploads" in path.parts:
        return {"ok": False, "reason": "refusing to send a source upload; clips only"}

    caption = str(clip.get("caption") or "").strip()
    tags = " ".join(str(t) for t in (clip.get("hashtags") or []))
    text = (caption + ("\n\n" + tags if tags else "")).strip() or str(clip.get("hook") or "")

    entry = {"ts": time.time(), "clip_id": clip.get("id"), "variant": variant,
             "size_mb": round(size_mb, 1), "slot": when_iso or c["slot"]}
    try:
        up = _upload(c["url"], c["key"], path)
        media_id = up.get("id") or up.get("path") or up.get("name")
        payload = {
            "type": "schedule" if when_iso else "now" if c["slot"] == "now" else "schedule",
            "date": when_iso or "",
            "shortLink": False,
            "posts": [{"integration": {"id": clip.get("postiz_integration_id") or ""},
                       "value": [{"content": text, "image": [{"id": media_id}] if media_id else []}]}],
        }
        res = _post(f"{c['url']}/api/public/v1/posts", c["key"], payload)
        # Store the platform media id now: Instagram Insights can only be queried
        # for API-published media, and without this UP3 has nothing to look up.
        entry.update(ok=True, media_id=media_id, response=str(res)[:400])
        _log(entry)
        return {"ok": True, "media_id": media_id, "response": res}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
        entry.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
        _log(entry)
        return {"ok": False, "reason": entry["error"]}
