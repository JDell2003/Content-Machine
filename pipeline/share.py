"""Temporary public URLs — the smallest hole that lets Instagram work.

Instagram's publishing API fetches the video from a URL you give it, so SOMETHING
has to be reachable from the internet. The question is only how much.

The answer here is: one file, one unguessable token, for a few minutes.

  * the token is 32 random url-safe chars from `secrets` — not a counter, not a
    hash of the clip id, nothing derivable from anything the user can see
  * it maps to exactly ONE file and expires (default 30 min), which is far more
    than the couple of minutes Meta needs to download
  * expired and unknown tokens are indistinguishable from the outside: both 404
  * tokens are revoked the moment publishing finishes

What is NOT exposed: the review UI, the clip list, the source recordings, the
LAN. The Cloudflare tunnel config forwards ONLY /share/* — every other path is
refused at the edge, before it reaches this machine. So even though the app
listens on one port, the public surface is a single route that serves finished
clips by random token.

Tokens live in memory on purpose. A restart invalidates every outstanding link,
which is the safe direction to fail.
"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path
from typing import Optional

DEFAULT_TTL_S = 30 * 60

_LOCK = threading.Lock()
_TOKENS: dict[str, dict] = {}


def _prune(now: Optional[float] = None) -> None:
    now = now or time.time()
    dead = [t for t, v in _TOKENS.items() if v["expires"] < now]
    for t in dead:
        _TOKENS.pop(t, None)


def mint(path: Path, ttl_s: int = DEFAULT_TTL_S, note: str = "") -> str:
    """-> an opaque token for this file. Caller builds the URL."""
    token = secrets.token_urlsafe(24)
    with _LOCK:
        _prune()
        _TOKENS[token] = {"path": str(Path(path).resolve()),
                          "expires": time.time() + max(60, int(ttl_s)),
                          "note": note, "hits": 0}
    return token


def resolve(token: str) -> Optional[Path]:
    """-> the file, or None. An expired token is indistinguishable from a wrong
    one: both come back None so the caller 404s identically."""
    with _LOCK:
        _prune()
        rec = _TOKENS.get(str(token or ""))
        if not rec:
            return None
        rec["hits"] += 1
    p = Path(rec["path"])
    return p if p.exists() else None


def revoke(token: str) -> None:
    with _LOCK:
        _TOKENS.pop(str(token or ""), None)


def active() -> list[dict]:
    """For the UI — never includes the token itself."""
    now = time.time()
    with _LOCK:
        _prune(now)
        return [{"note": v["note"], "hits": v["hits"],
                 "expires_in_s": int(v["expires"] - now)}
                for v in _TOKENS.values()]
