"""Publish to Instagram directly from the Content Machine. No Postiz.

WHY THIS REPLACED POSTIZ
Postiz is Postgres + Redis + Temporal + a Next.js app — about 1.2 GB of RAM and
8.5 GB of disk — to wrap what is, underneath, two HTTP calls to Meta. It also
crashed on this machine because the current image needs a Temporal service.
Running it on its own port meant a second address to remember for no benefit.

THE THING THAT DECIDES THE WHOLE ARCHITECTURE
Instagram's Content Publishing API does NOT accept a file upload. You hand it a
`video_url` and META'S SERVERS FETCH IT. That URL must be reachable from the
public internet. This is why a purely local setup cannot post to Instagram, with
Postiz or without it — the media has to be reachable, not just the app.

So exactly one thing needs to be exposed: a single, unguessable, short-lived URL
per clip. Not the app, not the LAN, not the review UI. See `share.py`.

THE FLOW (two calls, plus polling)
    1. POST /{ig_user_id}/media           video_url + caption -> creation_id
    2. GET  /{creation_id}?fields=status_code   poll until FINISHED
    3. POST /{ig_user_id}/media_publish   creation_id -> MEDIA ID

Step 3's media id is the thing UP3 needs: Instagram Insights can only be queried
for media the API published.

REELS: video posts go up as Reels (media_type=REELS). Meta requires MP4/MOV,
H.264 + AAC, under 1 GB, 3s-15min, and aspect between 0.01:1 and 10:1. The
renderer already produces exactly that.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

GRAPH = "https://graph.facebook.com/v21.0"
LOG = Path(__file__).resolve().parent.parent / "data" / "logs" / "instagram.jsonl"

# Meta's own limits, checked before we waste a round trip.
MAX_MB = 1000
MIN_SECONDS = 3
MAX_SECONDS = 15 * 60


def _log(entry: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = time.time()
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _call(url: str, params: dict, method: str = "GET", timeout: int = 120) -> dict:
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data.decode()}", method="GET")
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        # Meta puts the useful part in the BODY, not the status line. Surfacing
        # only "HTTP 400" would throw away the one sentence that explains why.
        body = exc.read().decode("utf-8", "replace")[:600]
        try:
            err = json.loads(body).get("error", {})
            msg = err.get("error_user_msg") or err.get("message") or body
        except json.JSONDecodeError:
            msg = body
        raise RuntimeError(f"Instagram: {msg}") from exc


def configured(cfg: dict) -> bool:
    return bool(cfg.get("ig_user_id") and cfg.get("access_token"))


def check_media(path: Path, duration_s: float) -> Optional[str]:
    """-> a reason string if Meta would reject this, else None."""
    if not path.exists():
        return "the rendered file is missing"
    mb = path.stat().st_size / 1_048_576
    if mb > MAX_MB:
        return f"{mb:.0f} MB is over Instagram's {MAX_MB} MB limit"
    if duration_s and duration_s < MIN_SECONDS:
        return f"{duration_s:.0f}s is under Instagram's {MIN_SECONDS}s minimum"
    if duration_s and duration_s > MAX_SECONDS:
        return f"{duration_s / 60:.0f} min is over Instagram's 15 min limit"
    return None


def publish(*, video_url: str, caption: str, cfg: dict,
            on_status: Optional[Callable[[str], None]] = None,
            poll_seconds: int = 300) -> dict:
    """Post one Reel. -> {ok, media_id|reason, permalink}. Never raises."""
    if not configured(cfg):
        return {"ok": False, "reason": "Instagram not connected "
                                       "(missing IG user id or access token)"}
    ig, token = cfg["ig_user_id"], cfg["access_token"]
    say = on_status or (lambda _m: None)

    try:
        say("handing Instagram the video URL…")
        created = _call(f"{GRAPH}/{ig}/media", {
            "media_type": "REELS", "video_url": video_url,
            "caption": caption[:2200], "access_token": token,
        }, method="POST")
        cid = created.get("id")
        if not cid:
            return {"ok": False, "reason": f"no creation id returned: {created}"}

        # Meta downloads and transcodes on their side; this can take minutes for
        # a long clip. Publishing before it's FINISHED fails, so we wait.
        say("Instagram is downloading and processing it…")
        deadline = time.time() + poll_seconds
        status = ""
        while time.time() < deadline:
            time.sleep(5)
            st = _call(f"{GRAPH}/{cid}", {"fields": "status_code,status",
                                          "access_token": token})
            status = st.get("status_code") or ""
            if status == "FINISHED":
                break
            if status == "ERROR":
                return {"ok": False, "reason": f"Instagram rejected it: "
                                               f"{st.get('status') or 'no detail'}"}
            say(f"Instagram status: {status or 'IN_PROGRESS'}")
        if status != "FINISHED":
            return {"ok": False, "reason": f"still {status or 'IN_PROGRESS'} after "
                                           f"{poll_seconds}s — not published"}

        say("publishing…")
        pub = _call(f"{GRAPH}/{ig}/media_publish",
                    {"creation_id": cid, "access_token": token}, method="POST")
        media_id = pub.get("id")
        if not media_id:
            return {"ok": False, "reason": f"publish returned no id: {pub}"}

        permalink = ""
        try:
            permalink = _call(f"{GRAPH}/{media_id}",
                              {"fields": "permalink", "access_token": token}
                              ).get("permalink", "")
        except RuntimeError:
            pass    # cosmetic only; never fail a real publish over it

        _log({"ok": True, "media_id": media_id, "permalink": permalink,
              "creation_id": cid})
        return {"ok": True, "media_id": media_id, "permalink": permalink}

    except (RuntimeError, urllib.error.URLError, OSError,
            json.JSONDecodeError) as exc:
        reason = f"{exc}"[:400]
        _log({"ok": False, "reason": reason})
        return {"ok": False, "reason": reason}


def insights(media_id: str, cfg: dict) -> dict:
    """Pull metrics for one published Reel — the UP3 half of the loop."""
    if not configured(cfg):
        return {}
    try:
        res = _call(f"{GRAPH}/{media_id}/insights", {
            "metric": "plays,reach,likes,comments,shares,saved,total_interactions",
            "access_token": cfg["access_token"]})
    except (RuntimeError, urllib.error.URLError, OSError, json.JSONDecodeError):
        return {}
    out: dict = {}
    for row in res.get("data", []):
        vals = row.get("values") or [{}]
        out[row.get("name")] = vals[0].get("value")
    # Map Meta's names onto the registry's.
    return {
        "views": out.get("plays"), "reach": out.get("reach"),
        "likes": out.get("likes"), "comments": out.get("comments"),
        "shares": out.get("shares"), "saves": out.get("saved"),
    }
