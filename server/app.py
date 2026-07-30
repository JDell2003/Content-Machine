"""Content Machine — local server. LAN only.

Reachable from devices on the same home WiFi. The socket binds 0.0.0.0 on purpose
rather than a literal LAN IP: DHCP can move this PC's address, and a hardcoded
bind would make the server refuse to start after a lease change. What actually
restricts access is the firewall rule (scoped to this subnet only, see
setup-windows.ps1) plus the PIN.
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import config, jobs, net, uploads

app = FastAPI(title="Content Machine", docs_url=None, redoc_url=None)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

OPEN_PATHS = {"/login", "/healthz"}


def _authed(request: Request) -> bool:
    return bool(request.session.get("ok"))


@app.middleware("http")
async def require_pin(request: Request, call_next):
    path = request.url.path
    if path in OPEN_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if not _authed(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "locked"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# Added AFTER require_pin on purpose. Starlette runs the last-added middleware
# outermost, so this has to be registered last for request.session to already be
# populated by the time the PIN check reads it.
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    session_cookie="cm_session",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=False,  # plain HTTP on the home LAN; no cert to terminate here
)


# ---------------------------------------------------------------- auth
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, bad: int = 0):
    if _authed(request):
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(request, "login.html", {"bad": bool(bad)})


@app.post("/login")
async def login_submit(request: Request, pin: str = Form("")):
    # compare_digest so a wrong PIN can't be probed by timing
    if hmac.compare_digest(str(pin).strip(), config.PIN):
        request.session["ok"] = True
        request.session["at"] = time.time()
        return RedirectResponse("/", status_code=303)
    time.sleep(0.6)  # take the edge off brute forcing
    return RedirectResponse("/login?bad=1", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return TEMPLATES.TemplateResponse(request, "upload.html", {})


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "status.html", {})


@app.get("/review", response_class=HTMLResponse)
async def review_index(request: Request):
    return TEMPLATES.TemplateResponse(request, "review.html", {"job": None})


@app.get("/review/{job_id}", response_class=HTMLResponse)
async def review_job(request: Request, job_id: str):
    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return TEMPLATES.TemplateResponse(request, "review.html", {"job": job})


# ---------------------------------------------------------------- upload api
@app.post("/api/upload/init")
async def upload_init(payload: dict):
    name = str(payload.get("name") or "video")
    size = int(payload.get("size") or 0)
    mtime = float(payload.get("mtime") or 0)
    chunk_size = int(payload.get("chunk_size") or (5 * 1024 * 1024))
    if size <= 0:
        raise HTTPException(400, "size required")
    if Path(name).suffix.lower() not in config.VIDEO_SUFFIXES:
        raise HTTPException(400, f"unsupported file type: {Path(name).suffix or 'none'}")
    return uploads.init(name, size, mtime, chunk_size)


@app.get("/api/upload/status")
async def upload_status(upload_id: str):
    m = uploads.meta(upload_id)
    if not m:
        raise HTTPException(404, "unknown upload")
    return {**m, "received": uploads.received(upload_id), "missing": uploads.missing(upload_id)}


@app.post("/api/upload/chunk")
async def upload_chunk(upload_id: str = Form(...), index: int = Form(...),
                       chunk: UploadFile = Form(...)):
    body = await chunk.read()
    if not body:
        raise HTTPException(400, "empty chunk")
    try:
        return uploads.save_chunk(upload_id, int(index), body)
    except FileNotFoundError:
        raise HTTPException(404, "unknown upload") from None


@app.post("/api/upload/complete")
async def upload_complete(payload: dict):
    upload_id = str(payload.get("upload_id") or "")
    m = uploads.meta(upload_id)
    if not m:
        raise HTTPException(404, "unknown upload")
    gaps = uploads.missing(upload_id)
    if gaps:
        return JSONResponse({"error": "incomplete", "missing": gaps[:64]}, status_code=409)
    profile = str(payload.get("profile") or "").upper()
    if profile not in {"BRAND", "TRAINER", "BOTH"}:
        raise HTTPException(400, "profile must be BRAND, TRAINER or BOTH")
    try:
        dest = uploads.assemble(upload_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    job = jobs.create(dest, m.get("name") or dest.name, profile=profile,
                      include_raw=bool(payload.get("include_raw")))
    return {"ok": True, "job_id": job["id"], "path": str(dest)}


@app.post("/api/upload/abandon")
async def upload_abandon(payload: dict):
    uploads.abandon(str(payload.get("upload_id") or ""))
    return {"ok": True}


# ---------------------------------------------------------------- job api
@app.get("/api/jobs")
async def api_jobs():
    return {"jobs": jobs.all_jobs()}


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str):
    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def api_job_cancel(job_id: str):
    job = jobs.cancel(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return job


@app.get("/api/jobs/{job_id}/log")
async def api_job_log(job_id: str):
    p = config.LOGS / f"{job_id}.log"
    if not p.exists():
        return {"log": ""}
    return {"log": p.read_text(encoding="utf-8", errors="replace")[-20000:]}


# ---------------------------------------------------------------- clip media
def _find_clip(job: dict, clip_id: str) -> dict:
    for clip in job.get("clips", []):
        if clip.get("id") == clip_id:
            return clip
    raise HTTPException(404, "no such clip")


def _clip_file(clip: dict, variant: str) -> Path:
    key = "path_vertical" if variant == "vertical" else "path"
    raw = clip.get(key) or clip.get("path")
    if not raw:
        raise HTTPException(404, "clip has no file")
    p = Path(raw)
    if not p.exists():
        raise HTTPException(404, "clip file missing from disk")
    return p


@app.get("/media/{job_id}/{clip_id}")
async def media(job_id: str, clip_id: str, request: Request, variant: str = "wide"):
    """Range-aware so the phone can scrub the preview without pulling the whole file."""
    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    path = _clip_file(_find_clip(job, clip_id), variant)
    total = path.stat().st_size
    ctype = mimetypes.guess_type(path.name)[0] or "video/mp4"

    rng = request.headers.get("range") or request.headers.get("Range")
    if not rng or not rng.startswith("bytes="):
        return FileResponse(path, media_type=ctype,
                            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"})

    spec = rng.split("=", 1)[1].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else total - 1
    except ValueError:
        raise HTTPException(416, "bad range") from None
    start = max(0, start)
    end = min(end, total - 1)
    if start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})

    def stream():
        remaining = end - start + 1
        with path.open("rb") as fh:
            fh.seek(start)
            while remaining > 0:
                block = fh.read(min(1024 * 512, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    return StreamingResponse(stream(), status_code=206, media_type=ctype, headers={
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "no-store",
    })


@app.get("/download/{job_id}/{clip_id}")
async def download(job_id: str, clip_id: str, variant: str = "wide"):
    """Original bytes, no transcode — this is the file that gets posted."""
    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    clip = _find_clip(job, clip_id)
    path = _clip_file(clip, variant)
    stem = Path(job.get("original_name") or "clip").stem[:40]
    label = f"{stem}_{clip.get('rank', 0):02d}_{variant}{path.suffix}"
    return FileResponse(path, media_type="application/octet-stream", filename=label,
                        headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- approvals
REJECT_REASONS = {"bad_cut", "boring", "wrong_profile", "bad_caption", "other"}


def _log_feedback(job: dict, clip: dict, reason: str, note: str) -> None:
    """Rejection memory. Append-only so `tune` can read the whole history later;
    carries enough clip metadata that a pattern is visible without the video."""
    rec = {
        "ts": time.time(), "job_id": job.get("id"), "clip_id": clip.get("id"),
        "profile": clip.get("profile"), "profile_tag": clip.get("profile_tag"),
        "reason": reason, "note": note[:500],
        "score": clip.get("score"), "structure": clip.get("structure"),
        "duration_s": clip.get("duration_s"), "timestamp": clip.get("timestamp"),
        "hook": clip.get("hook"), "caption": clip.get("caption"),
        "review_verdict": clip.get("review_verdict"),
        "audio_ok": clip.get("audio_ok"), "audio_note": clip.get("audio_note"),
        "transcript": str(clip.get("transcript") or "")[:1200],
    }
    try:
        with (config.ROOT / "feedback.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


@app.post("/api/clips/{job_id}/{clip_id}/decision")
async def clip_decision(job_id: str, clip_id: str, payload: dict):
    verdict = str(payload.get("verdict") or "").lower()
    if verdict not in {"approved", "rejected", "pending"}:
        raise HTTPException(400, "verdict must be approved|rejected|pending")
    reason = str(payload.get("reason") or "").lower()
    note = str(payload.get("note") or "")
    # A rejection without a reason teaches the machine nothing, so it's refused.
    if verdict == "rejected" and reason not in REJECT_REASONS:
        raise HTTPException(400, f"reason required, one of {sorted(REJECT_REASONS)}")
    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")

    copied = []
    for clip in job.get("clips", []):
        if clip.get("id") != clip_id:
            continue
        clip["decision"] = verdict
        clip["decided_at"] = time.time()
        if verdict == "rejected":
            clip["reject_reason"] = reason
            clip["reject_note"] = note[:500]
            _log_feedback(job, clip, reason, note)
        if verdict == "approved":
            for variant, key in (("wide", "path"), ("vertical", "path_vertical")):
                src = clip.get(key)
                if not src or not Path(src).exists():
                    continue
                stem = Path(job.get("original_name") or "clip").stem[:40]
                dest = config.APPROVED / f"{stem}_{clip.get('rank', 0):02d}_{variant}{Path(src).suffix}"
                if not dest.exists():
                    shutil.copy2(src, dest)  # copy, not move: preview must keep working
                copied.append(str(dest))
            clip["approved_copies"] = copied
        break
    else:
        raise HTTPException(404, "no such clip")

    jobs.update(job_id, clips=job["clips"])
    return {"ok": True, "verdict": verdict, "copied": copied}


@app.get("/api/net")
async def api_net():
    """Current LAN URL, so the status page can show the bookmark and flag a
    DHCP-induced address change without anyone reading a log."""
    return {**net.urls(), "mdns": net.try_enable_mdns()}


@app.get("/api/approved")
async def api_approved():
    items = []
    for p in sorted(config.APPROVED.glob("*")):
        if p.is_file():
            items.append({"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime})
    return {"dir": str(config.APPROVED), "items": items}


# ---------------------------------------------------------------- boot
@app.on_event("startup")
async def _startup():
    uploads.sweep()
    print(net.banner(), flush=True)
    print(f"  (also written to {net.write_url_file()})\n", flush=True)
    from . import pipeline_bridge  # noqa: PLC0415 - late import so a pipeline
    jobs.set_runner(pipeline_bridge.run)  # import error can't stop the server booting
    jobs.start_worker()
