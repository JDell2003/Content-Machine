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
import subprocess
import mimetypes
import shutil
import time
from pathlib import Path

from fastapi import Body, FastAPI, Form, HTTPException, Request, Response, UploadFile
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


@app.post("/api/jobs/{job_id}/retry")
async def api_job_retry(job_id: str):
    fresh = jobs.retry(job_id)
    if not fresh:
        raise HTTPException(404, "no such job, or its source video is gone")
    return fresh


@app.post("/api/jobs/start")
async def api_job_start(payload: dict):
    """Queue a job from a file already on disk - no re-upload.

    The transcript is cached beside the source, so a run started this way skips
    the slowest stage entirely and goes straight to ranking.
    """
    name = str(payload.get("name") or "").strip()
    src = config.UPLOADS / Path(name).name       # basename only: no path escapes
    if not name or not src.exists() or src.suffix.lower() not in config.VIDEO_SUFFIXES:
        raise HTTPException(404, "no such video on disk")
    profile = str(payload.get("profile") or "").upper()
    if profile not in {"BRAND", "TRAINER", "BOTH"}:
        raise HTTPException(400, "profile must be BRAND, TRAINER or BOTH")
    job = jobs.create(src, src.name, profile=profile,
                      include_raw=bool(payload.get("include_raw")))
    return {"ok": True, "job_id": job["id"]}


@app.get("/api/files")
async def api_files():
    """Raw uploads still on disk, with how long until retention removes them."""
    from pipeline import retention  # noqa: PLC0415
    items = []
    for p in sorted(config.UPLOADS.glob("*"), key=lambda x: -x.stat().st_mtime):
        if not p.is_file() or p.suffix.lower() not in config.VIDEO_SUFFIXES:
            continue
        age_h = (time.time() - p.stat().st_mtime) / 3600
        items.append({
            "name": p.name, "gb": round(p.stat().st_size / 1e9, 2),
            "age_hours": round(age_h, 1),
            "deletes_in_hours": round(max(0.0, retention.SOURCE_MAX_AGE_H - age_h), 1),
            "has_transcript": any(config.UPLOADS.glob(f"{p.stem}*.transcript.json")),
        })
    return {"files": items, "usage": retention.usage()}


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
    key = {"vertical": "path_vertical", "reframed": "path_reframed"}.get(variant, "path")
    raw = clip.get(key) or clip.get("path")
    if not raw:
        raise HTTPException(404, "clip has no file")
    p = Path(raw)
    if not p.exists():
        raise HTTPException(404, "clip file missing from disk")
    return p


# -------------------------------------------------------- look preview/apply
def _preview_src(job: dict, clip: dict) -> tuple[Path, float]:
    """Preview must come from the UNGRADED original. The finished clip already
    has a grade baked in, so previewing on it would show two grades stacked."""
    from pipeline import relook as _rl  # noqa: PLC0415
    src = _rl.source_for(job)
    if src is not None:
        # Middle of the clip — more representative than the first frame, which is
        # often mid-blink or mid-cut.
        return src, float(clip["start_s"]) + float(clip["duration_s"]) / 2
    return Path(clip["path"]), float(clip.get("duration_s", 2)) / 2


@app.get("/api/preview/frame")
async def preview_frame(job_id: str, clip_id: str, preset: str = "office",
                        intensity: float = 1.0, width: int = 720):
    """One graded still, straight from the source. Fast enough to drag a slider."""
    from pipeline import color as _c  # noqa: PLC0415
    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    clip = _find_clip(job, clip_id)
    src, at = _preview_src(job, clip)

    vf = [f"scale={max(240, min(1280, int(width)))}:-2"]
    chain = _c.fast_filter_chain(preset, float(intensity))
    if chain:
        vf.append(chain)
    dest = config.DATA / "tmp" / f"prev_{job_id}_{clip_id}_{preset}_{intensity}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [shutil.which("ffmpeg") or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1",
            "-vf", ",".join(vf), "-q:v", "3", str(dest)]
    p = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if p.returncode != 0 or not dest.exists():
        raise HTTPException(500, f"preview failed: {(p.stderr or '')[-200:]}")
    return FileResponse(dest, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/preview/audio")
async def preview_audio(job_id: str, clip_id: str, preset: str = "office",
                        intensity: float = 1.0, seconds: int = 12):
    """A short audio sample with the voice chain applied, from the raw source."""
    from pipeline import audio_fx as _a  # noqa: PLC0415
    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    clip = _find_clip(job, clip_id)
    src, at = _preview_src(job, clip)
    dur = max(4, min(30, int(seconds)))
    at = max(0.0, at - dur / 2)

    chain, _ = _a.build_chain(src, at, dur, preset=preset, intensity=float(intensity))
    dest = config.DATA / "tmp" / f"prevaud_{job_id}_{clip_id}_{preset}_{intensity}.m4a"
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [shutil.which("ffmpeg") or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{at:.3f}", "-i", str(src), "-t", f"{dur}", "-map", "0:a:0", "-vn",
            "-af", chain, "-c:a", "aac", "-b:a", "160k", str(dest)]
    p = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if p.returncode != 0 or not dest.exists():
        raise HTTPException(500, f"audio preview failed: {(p.stderr or '')[-200:]}")
    return FileResponse(dest, media_type="audio/mp4",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/{job_id}/relook")
async def relook_plan(job_id: str, scope: str = "all"):
    from pipeline import relook as _rl  # noqa: PLC0415
    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return {**_rl.plan(job, scope), "running": _rl.status(job_id),
            "counts": {s: len(_rl.select(job, s)) for s in ("all", "keeping", "approved")}}


@app.post("/api/jobs/{job_id}/relook")
async def relook_apply(job_id: str, body: dict = Body(...)):
    """Apply a look to one clip or the whole job. Whole-job runs in the
    background and writes progress into the job as each clip lands."""
    from pipeline import look as _l, relook as _rl  # noqa: PLC0415
    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")

    look = {**_l.load(), **{k: v for k, v in (body or {}).items()
                            if k in _l.KEYS and v is not None}}
    if body.get("save_default"):
        _l.save(look)

    if body.get("scope") == "job":
        return _rl.run_all(job_id, look, jobs.load, jobs.update,
                           scope=str(body.get("clip_scope") or "all"))

    clip = _find_clip(job, str(body.get("clip_id") or ""))
    _rl.one(job, clip, look)
    jobs.update(job_id, clips=job["clips"])
    if clip.get("relook_error"):
        raise HTTPException(500, clip["relook_error"])
    return {"ok": True, "clip": clip.get("id"), "grade": clip.get("grade"),
            "audio_fx": clip.get("audio_fx")}


@app.delete("/api/jobs/{job_id}/relook")
async def relook_cancel(job_id: str):
    from pipeline import relook as _rl  # noqa: PLC0415
    return {"cancelled": _rl.cancel(job_id)}


# ------------------------------------------------------------------ schedule
@app.get("/api/schedule")
async def schedule_get(days: int = 14):
    from pipeline import schedule as _s  # noqa: PLC0415
    return {**_s.build(jobs.all_jobs(), days_ahead=max(1, min(60, days))),
            "phases": {k: v["label"] for k, v in _s.PHASES.items()}}


@app.post("/api/schedule")
async def schedule_set(body: dict = Body(...)):
    from pipeline import schedule as _s  # noqa: PLC0415
    _s.save(body or {})
    return {**_s.build(jobs.all_jobs()),
            "phases": {k: v["label"] for k, v in _s.PHASES.items()}}


@app.post("/api/schedule/posted")
async def schedule_mark_posted(body: dict = Body(...)):
    """Mark a clip as actually posted so it leaves the queue and the ones behind
    it move up. Nothing here publishes — this only records what you did."""
    job = jobs.load(str(body.get("job_id") or ""))
    if not job:
        raise HTTPException(404, "no such job")
    clip = _find_clip(job, str(body.get("clip_id") or ""))
    clip["posted"] = bool(body.get("posted", True))
    clip["posted_at"] = time.time() if clip["posted"] else None
    jobs.update(job["id"], clips=job["clips"])
    from pipeline import schedule as _s  # noqa: PLC0415
    return _s.build(jobs.all_jobs())


@app.post("/api/schedule/pin")
async def schedule_pin(body: dict = Body(...)):
    """Drag-to-reorder: pin a clip to an explicit slot, or clear the pin so it
    rejoins the flowing queue. A pinned clip keeps its slot when others move."""
    job = jobs.load(str(body.get("job_id") or ""))
    if not job:
        raise HTTPException(404, "no such job")
    clip = _find_clip(job, str(body.get("clip_id") or ""))
    at = str(body.get("at") or "").strip()
    if at:
        clip["scheduled_at"] = at
    else:
        clip.pop("scheduled_at", None)
    jobs.update(job["id"], clips=job["clips"])
    from pipeline import schedule as _s  # noqa: PLC0415
    return _s.build(jobs.all_jobs())


@app.post("/api/schedule/unschedule")
async def schedule_unschedule(body: dict = Body(...)):
    """Take a clip out of the queue entirely by moving it back to pending."""
    job = jobs.load(str(body.get("job_id") or ""))
    if not job:
        raise HTTPException(404, "no such job")
    clip = _find_clip(job, str(body.get("clip_id") or ""))
    clip["decision"] = "pending"
    clip.pop("approved_at", None)
    clip.pop("scheduled_at", None)
    jobs.update(job["id"], clips=job["clips"])
    from pipeline import schedule as _s  # noqa: PLC0415
    return _s.build(jobs.all_jobs())


@app.get("/swipe", response_class=HTMLResponse)
async def swipe_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "swipe.html", {})


@app.get("/api/swipe/queue")
async def swipe_queue(limit: int = 40):
    """Undecided clips across every job, best first — the deck to swipe through."""
    deck = []
    for job in jobs.all_jobs():
        for clip in job.get("clips", []):
            if clip.get("decision") not in (None, "", "pending") or not clip.get("path"):
                continue
            deck.append({
                "job_id": job.get("id"), "id": clip.get("id"),
                "rank": clip.get("rank"), "score": clip.get("score"),
                "hook": clip.get("hook"), "caption": clip.get("caption"),
                "hashtags": clip.get("hashtags") or [],
                "duration_s": clip.get("duration_s"),
                "start_s": clip.get("start_s"),
                "profile": clip.get("profile"), "topic": clip.get("topic"),
                "structure": clip.get("structure"),
                "why": clip.get("why"), "review_verdict": clip.get("review_verdict"),
                "review_reason": clip.get("review_reason"),
                "audio_ok": clip.get("audio_ok"), "audio_note": clip.get("audio_note"),
                "source_name": job.get("original_name"),
            })
    # Deliberately NOT score order. Clips are cut from one long recording, so the
    # highest scorers cluster in whichever stretch was strongest — you'd swipe
    # through ten takes on the same idea in a row, approve a few, and they'd all
    # go out the same week saying the same thing.
    #
    # Shuffling alone doesn't fix it either: random still deals same-topic cards
    # back to back often enough to notice. So shuffle, then de-cluster — walk the
    # deck and push a card back if it repeats the topic or the 5-minute stretch of
    # source the one before it came from.
    import random  # noqa: PLC0415
    random.shuffle(deck)

    def _key(c: dict) -> tuple:
        return ((c.get("topic") or c.get("structure") or "?"),
                int((c.get("start_s") or 0) // 300))

    spread: list[dict] = []
    held: list[dict] = []
    while deck or held:
        pool = deck or held
        if pool is held:
            deck, held = held, []
            pool = deck
        c = pool.pop(0)
        if spread and _key(c) == _key(spread[-1]) and (deck or held):
            held.append(c)          # same meat as the last card — try again later
            continue
        spread.append(c)

    return {"deck": spread[:max(1, min(200, limit))], "total": len(spread)}


# ------------------------------------------------------------ post registry
@app.get("/api/posts")
async def posts_list():
    from pipeline import registry as _r  # noqa: PLC0415
    return {"posts": _r.posts()}


@app.get("/api/patterns")
async def posts_patterns(metric: str = "views"):
    from pipeline import registry as _r  # noqa: PLC0415
    return _r.patterns(metric if metric in _r.METRIC_KEYS else "views")


@app.post("/api/posts/published")
async def posts_published(body: dict = Body(...)):
    """Record that a clip went out. Called by the publish path when Postiz is
    wired; usable by hand until then so the registry starts filling now."""
    from pipeline import registry as _r  # noqa: PLC0415
    job = jobs.load(str(body.get("job_id") or ""))
    if not job:
        raise HTTPException(404, "no such job")
    clip = _find_clip(job, str(body.get("clip_id") or ""))
    row = _r.record_published(
        job_id=job["id"], clip_id=clip["id"],
        platform=str(body.get("platform") or "instagram"),
        media_id=str(body.get("media_id") or ""),
        permalink=str(body.get("permalink") or ""),
        slot_at=str(body.get("slot_at") or clip.get("scheduled_at") or ""),
        hook=clip.get("hook") or "", source_name=job.get("original_name") or "",
        topic=clip.get("topic") or "", profile=clip.get("profile") or "")
    clip["posted"] = True
    clip["posted_at"] = time.time()
    if row.get("media_id"):
        clip["media_id"] = row["media_id"]      # the handle UP3 metrics pull needs
    jobs.update(job["id"], clips=job["clips"])
    return {"ok": True, "row": row}


@app.post("/api/posts/metrics")
async def posts_metrics(body: dict = Body(...)):
    """Attach a metrics reading. Append-only: a later pull never overwrites an
    earlier one, because hour-one and week-two numbers mean different things."""
    from pipeline import registry as _r  # noqa: PLC0415
    vals = {k: body[k] for k in _r.METRIC_KEYS if k in body}
    if not vals:
        raise HTTPException(400, f"send at least one of {list(_r.METRIC_KEYS)}")
    return {"ok": True, "row": _r.record_metrics(
        job_id=str(body.get("job_id") or ""), clip_id=str(body.get("clip_id") or ""),
        **vals)}


@app.get("/patterns", response_class=HTMLResponse)
async def patterns_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "patterns.html", {})


# ---------------------------------------------------------------------- tune
@app.get("/api/tune")
async def tune_signal():
    from pipeline import tune as _t  # noqa: PLC0415
    return _t.signal()


@app.post("/api/tune")
async def tune_run(body: dict = Body(default={})):
    """One brain call. Refuses on thin data unless forced — a confident rubric
    built from four rejections biases every future run."""
    from pipeline import tune as _t  # noqa: PLC0415
    return _t.run(force=bool((body or {}).get("force")))


# --------------------------------------------------------------- look & sound
@app.get("/api/look")
async def look_get():
    from pipeline import color as _c, audio_fx as _a, look as _l  # noqa: PLC0415
    return {**_l.load(),
            "color_presets": {k: v["label"] for k, v in _c.PRESETS.items()},
            "audio_presets": {k: v["label"] for k, v in _a.PRESETS.items()}}


@app.post("/api/look")
async def look_set(body: dict = Body(...)):
    from pipeline import look as _l  # noqa: PLC0415
    return _l.save(body or {})


# ------------------------------------------------------------------ captions
@app.get("/api/captions/settings")
async def caption_settings():
    from pipeline import captions as _cap  # noqa: PLC0415
    return _cap.load()


@app.post("/api/captions/settings")
async def caption_settings_save(body: dict = Body(...)):
    """Global style + the spelling dictionary. Takes effect on the next render;
    existing clips keep their burned-in pixels until they're re-rendered."""
    from pipeline import captions as _cap  # noqa: PLC0415
    return _cap.save(body or {})


@app.get("/api/clips/{job_id}/{clip_id}/captions")
async def clip_captions(job_id: str, clip_id: str):
    from pipeline import captions as _cap  # noqa: PLC0415
    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    clip = _find_clip(job, clip_id)
    srt = Path(clip["path"]).with_suffix(".srt") if clip.get("path") else None
    return {"cues": _cap.read_srt(srt) if srt else [], "srt": str(srt) if srt else None}


@app.post("/api/clips/{job_id}/{clip_id}/captions")
async def clip_captions_save(job_id: str, clip_id: str, body: dict = Body(...)):
    """Rewrite this clip's .srt and re-burn it.

    Burned-in text is pixels — there is no way to change it without re-encoding,
    so this is the one operation that knowingly costs a generation. It re-cuts
    from the cached source segment when that still exists, which keeps the
    generation count at one rather than two.
    """
    from pipeline import captions as _cap, render as _r  # noqa: PLC0415

    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    clip = _find_clip(job, clip_id)
    if not clip.get("path"):
        raise HTTPException(400, "clip was never rendered")

    out = Path(clip["path"])
    srt = out.with_suffix(".srt")
    cues = body.get("cues")
    if cues:
        _cap.write_srt(srt, cues)
    if not srt.exists():
        raise HTTPException(400, "this clip has no caption file to edit")

    # Prefer the lossless pre-cut segment; falling back to the finished clip
    # would stack a second generation of compression on it.
    seg = out.with_name(out.stem + "_src.mp4")
    if seg.exists():
        src, start, dur = seg, 1.0, float(clip["duration_s"])
    elif Path(job.get("source_path") or "").exists():
        src, start, dur = Path(job["source_path"]), float(clip["start_s"]), float(clip["duration_s"])
    else:
        src, start, dur = out, 0.0, float(clip["duration_s"])

    style = body.get("style") or None
    tmp = out.with_name(out.stem + "_recap.mp4")
    try:
        _r.render_wide(src, tmp, start, start + dur,
                       subs_path=srt, grade=body.get("grade") or "",
                       style_override=style)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"re-render failed: {str(exc)[:300]}") from exc

    # The outro lives on the end of the old file and was just thrown away.
    if clip.get("outro"):
        try:
            from pipeline import outro as _o  # noqa: PLC0415
            geo = _o.probe(tmp)
            baked = _o.bake(geo["w"], geo["h"], geo["fps"],
                            codec_args=_r._video_codec_args(),
                            audio_args=_r._audio_args())
            if baked:
                _o.append(tmp, baked)
        except Exception as exc:  # noqa: BLE001
            clip["outro_error"] = f"{type(exc).__name__}: {exc}"[:200]

    out.unlink(missing_ok=True)
    tmp.replace(out)
    clip["captions_edited"] = True
    clip.pop("path_reframed", None)   # the old crop was of the old pixels
    clip.pop("reframe", None)
    jobs.update(job_id, clips=job["clips"])
    return {"ok": True, "cues": _cap.read_srt(srt)}


# --------------------------------------------------------------- CTA outro
@app.get("/api/outro")
async def outro_info():
    from pipeline import outro as _o  # noqa: PLC0415
    return _o.info()


@app.post("/api/outro")
async def outro_upload(video: UploadFile = Form(...)):
    """Straight multipart, not the chunked path — a CTA is seconds long, and the
    resumable machinery is for hour-long 4K sources."""
    from pipeline import outro as _o  # noqa: PLC0415

    if Path(video.filename or "").suffix.lower() not in config.VIDEO_SUFFIXES:
        raise HTTPException(400, "that isn't a video file")
    tmp = config.DATA / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    staged = tmp / f"outro_upload{Path(video.filename or 'x.mp4').suffix.lower()}"
    with staged.open("wb") as fh:
        shutil.copyfileobj(video.file, fh)
    try:
        return {"ok": True, **_o.install(staged, video.filename or "")}
    except ValueError as exc:
        staged.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from None


@app.delete("/api/outro")
async def outro_delete():
    from pipeline import outro as _o  # noqa: PLC0415
    _o.remove()
    return {"ok": True}


@app.get("/media/outro")
async def outro_preview():
    from pipeline import outro as _o  # noqa: PLC0415
    if not _o.SOURCE.exists():
        raise HTTPException(404, "no outro installed")
    return FileResponse(_o.SOURCE, media_type="video/mp4",
                        headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------- reframe
@app.post("/api/clips/{job_id}/{clip_id}/reframe")
async def reframe_clip(job_id: str, clip_id: str, body: dict = Body(...)):
    """Crop a finished clip to a chosen aspect ratio and save it alongside.

    Deliberately non-destructive: the 16:9 original is never overwritten, so a
    bad crop costs one re-crop, not the clip.
    """
    from pipeline import reframe as rf  # noqa: PLC0415 - keeps startup light

    job = jobs.load(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    clip = _find_clip(job, clip_id)
    src = _clip_file(clip, "wide")

    aspect = str(body.get("aspect") or "4:5")
    if aspect not in rf.ASPECTS:
        raise HTTPException(400, f"aspect must be one of {sorted(rf.ASPECTS)}")

    try:
        src_w, src_h = rf.probe(src)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise HTTPException(500, f"could not read clip: {exc}") from exc

    box = rf.plan(src_w, src_h, aspect,
                  zoom=float(body.get("zoom", 1.0)),
                  cx=float(body.get("cx", 0.5)),
                  cy=float(body.get("cy", 0.5)))
    if box is None:      # "original" — nothing to do
        clip.pop("path_reframed", None)
        clip.pop("reframe", None)
        jobs.update(job_id, clips=job["clips"])
        return {"ok": True, "aspect": "original", "reframed": False}

    if body.get("preview"):
        return {"ok": True, "box": box, "caption_safe": rf.caption_safe(box)}

    dest = src.with_name(src.stem + "_reframed.mp4")
    try:
        rf.apply(src, dest, box)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise HTTPException(500, f"crop failed: {str(exc)[:300]}") from exc

    clip["path_reframed"] = str(dest)
    clip["reframe"] = {**box, "caption_safe": rf.caption_safe(box)}
    jobs.update(job_id, clips=job["clips"])
    return {"ok": True, "box": box, "caption_safe": rf.caption_safe(box),
            "reframed": True}


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
        # The schedule queues in the order you said yes, so the first approval
        # timestamp has to survive later edits. Un-approving clears it, which is
        # what releases the slot and pulls everything behind it forward.
        if verdict == "approved":
            clip.setdefault("approved_at", clip["decided_at"])
        else:
            clip.pop("approved_at", None)
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


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "chat.html", {})


@app.get("/api/chat/threads")
async def chat_threads():
    from pipeline import chat  # noqa: PLC0415 - keeps the server bootable without it
    return {"threads": chat.list_threads()}


@app.post("/api/chat/threads")
async def chat_new_thread(payload: dict):
    from pipeline import chat  # noqa: PLC0415
    return chat.new_thread(str(payload.get("title") or ""))


@app.get("/api/chat/threads/{tid}")
async def chat_get_thread(tid: str):
    from pipeline import chat  # noqa: PLC0415
    t = chat.load_thread(tid)
    if not t:
        raise HTTPException(404, "no such thread")
    return t


@app.post("/api/chat/threads/{tid}/send")
async def chat_send(tid: str, payload: dict):
    from pipeline import chat  # noqa: PLC0415
    t = chat.load_thread(tid)
    if not t:
        raise HTTPException(404, "no such thread")
    msg = str(payload.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "empty message")
    try:
        return chat.send(t, msg, config)
    except Exception as exc:  # noqa: BLE001 - surface the reason, don't 500 blankly
        raise HTTPException(502, f"brain call failed: {str(exc)[:200]}") from None


@app.post("/api/chat/threads/{tid}/diff/{diff_id}")
async def chat_diff(tid: str, diff_id: str, payload: dict):
    from pipeline import chat  # noqa: PLC0415
    t = chat.load_thread(tid)
    if not t:
        raise HTTPException(404, "no such thread")
    res = chat.apply_diff(t, diff_id, bool(payload.get("approve")),
                          str(payload.get("note") or ""))
    if not res.get("ok"):
        raise HTTPException(400, res.get("reason") or "could not apply")
    return res


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
