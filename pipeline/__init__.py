"""Pipeline orchestrator: source video -> ranked, captioned, rendered clips.

Call budget per video, per profile (the fixed ~22.7k harness context per call is
what makes this matter):
    ranking : <=3 calls  (candidates split into at most 3 batches)
    captions:   1 call   (all surviving clips in one request)
    vision  : <=2 calls  (Upgrade Pass 2, batched across clips)
"""
from __future__ import annotations

import json
import math
import time
import uuid
from pathlib import Path
from typing import Callable

from . import (audio_fx, audio_gate, brain, color, edit, outro, prompts,
               render, segment, transcribe)
from . import look as _look
from . import tune
from . import autotune as _auto

MAX_RANK_CALLS = 3
# Clips render in parallel. Measured per clip: ~2s pre-cut + ~6s loudness
# analysis + ~35s encode. The encode is NVENC (GPU) but the audio filter chain
# (arnndn denoise, EQ, compressor) is CPU-bound, so serial rendering pegged one
# core and left the other 15 - and most of the encoder - idle.
RENDER_WORKERS = 3
# How many clips actually get RENDERED. At the default cadence (2/day) this is
# ten days of runway. It was 60, which meant rendering ~39 clips that would
# never be posted — about 45 minutes of encoding thrown away every run.
MAX_RENDER_PER_PROFILE = 21
# How many survive the first ranking pass and go into the shortlist call. Wider
# than the final cut on purpose: the second pass needs a real field to choose
# from, and reading 60 transcripts is one cheap call against 39 wasted encodes.
SHORTLIST_POOL = 60
SCORE_FLOOR = 45


def _cancelled(job_id: str) -> bool:
    """Cheap disk read. Called between every expensive stage so a cancel lands in
    seconds instead of waiting out a 7-minute caption call."""
    from server import jobs as _j  # noqa: PLC0415
    return bool((_j.load(job_id) or {}).get("cancel_requested"))


def _stop(job_id: str, report, clips: list) -> None:
    from server import jobs as _j  # noqa: PLC0415
    report(stage="cancelled", message=f"Cancelled ({len(clips)} clips kept)")
    _j.update(job_id, status=_j.CANCELLED, finished_at=time.time(), clips=clips)


def _profiles_for(job: dict) -> tuple[list[str], bool]:
    want = str(job.get("profile") or "BRAND").upper()
    raw = bool(job.get("include_raw"))
    if want == "BOTH":
        return ["BRAND", "TRAINER"], raw
    if want in prompts.PROFILES:
        return [want], raw
    return ["BRAND"], raw


def _diversify(ranked: list[dict], limit: int, *, bucket_s: float = 300.0,
               per_bucket: int = 4, per_topic: int = 3) -> list[dict]:
    """Pick the top N with spread, not just the top N by score.

    Straight score-ordering pulled clip after clip out of whichever five minutes
    happened to be strongest, so a 67-minute conversation came back looking like
    one topic. Repetition of a GOOD idea is wanted ("same meat, different cuts")
    - a feed of nothing else is not.

    Two soft caps, applied in score order:
      * at most `per_bucket` clips from any 5-minute stretch of the source
      * at most `per_topic` clips sharing a structure tag (teach-aha, story...)

    Both are soft: if the caps leave the list short, the best of the rejected
    are added back rather than shipping fewer clips than asked for.
    """
    chosen: list[dict] = []
    overflow: list[dict] = []
    bucket_count: dict[int, int] = {}
    topic_count: dict[str, int] = {}

    for c in ranked:
        b = int(c["start_s"] // bucket_s)
        topic = (c.get("structure") or "other").lower()
        if bucket_count.get(b, 0) >= per_bucket or topic_count.get(topic, 0) >= per_topic:
            overflow.append(c)
            continue
        chosen.append(c)
        bucket_count[b] = bucket_count.get(b, 0) + 1
        topic_count[topic] = topic_count.get(topic, 0) + 1
        if len(chosen) >= limit:
            return chosen

    # Caps were tighter than the target. Backfill in ROUNDS, taking the best
    # remaining from each bucket in turn, so raising the ceiling still spreads
    # the extras instead of dumping them all back into the busiest stretch.
    by_bucket: dict[int, list[dict]] = {}
    for c in overflow:
        by_bucket.setdefault(int(c["start_s"] // bucket_s), []).append(c)
    while len(chosen) < limit and any(by_bucket.values()):
        for b in sorted(by_bucket):
            if len(chosen) >= limit:
                break
            if by_bucket[b]:
                chosen.append(by_bucket[b].pop(0))
    return chosen


def _batches(items: list, n: int) -> list[list]:
    if not items:
        return []
    n = max(1, min(n, len(items)))
    size = math.ceil(len(items) / n)
    return [items[i:i + size] for i in range(0, len(items), size)]


def run_pipeline(*, job: dict, info: dict, report: Callable[..., None], cfg) -> None:
    src = Path(job["source_path"])
    job_id = job["id"]
    profiles, raw_addon = _profiles_for(job)

    fw = prompts.load_frameworks(cfg.FRAMEWORKS_PATH)
    exemplars = prompts.load_exemplars(cfg.ROOT / "EXEMPLARS.md")

    # ---- 1. transcribe (once, shared by every profile) ----------------------
    report(stage="transcribe", progress=0.08, message="Transcribing")

    def t_report(message: str = "", progress_hint: float = None, **_):
        report(stage="transcribe",
               progress=0.08 + 0.32 * (progress_hint or 0.0) if progress_hint else None,
               message=message or None)

    tr = transcribe.transcribe(src, model_size=cfg.WHISPER_MODEL,
                               device=cfg.WHISPER_DEVICE, compute=cfg.WHISPER_COMPUTE,
                               models_dir=cfg.MODELS / "whisper", report=t_report)
    if not tr.get("segments"):
        raise RuntimeError("no speech found in this recording")
    report(stage="transcribe", progress=0.40,
           message=f"{len(tr['segments'])} segments on {tr['device']}",
           transcript_device=tr["device"])

    # ---- 2. candidates + audio gate ---------------------------------------
    report(stage="candidates", progress=0.42, message="Finding complete-thought windows")
    cands = segment.build_candidates(tr, min_s=cfg.CLIP_MIN_S, max_s=cfg.CLIP_MAX_S,
                                     max_candidates=320, overlap_frac=0.92)
    if not cands:
        raise RuntimeError("no complete-thought windows of the right length were found")

    for c in cands:
        c["audio"] = audio_gate.analyse(src, c["start_s"], c["end_s"])
    flagged = sum(1 for c in cands if not c["audio"]["ok"])
    report(stage="candidates", progress=0.50,
           message=f"{len(cands)} candidates ({flagged} with audio warnings)",
           candidate_count=len(cands))

    # Auto grade is measured ONCE for the whole recording, not per clip: it is
    # the same room and the same camera throughout, and re-deriving it 21 times
    # would cost 21 measurement passes for an identical answer.
    auto_grade_filter = None
    if str(_look.load().get("color_preset", "")).lower() == "auto":
        report(stage="candidates", message="Measuring the footage to derive the grade")
        try:
            res = _auto.auto_grade(src, at=min(60.0, (info.get("duration_s") or 120) / 3))
            auto_grade_filter = res.get("filter") or ""
            b, a = res.get("before"), res.get("after")
            if b and a:
                report(message=("Grade derived: cast "
                                f"{b['cast']:+.0f} -> {a['cast']:+.0f}, "
                                f"black {b['black']:.0f} -> {a['black']:.0f}, "
                                f"contrast {b['contrast']:.0f} -> {a['contrast']:.0f}"))
        except Exception as exc:  # noqa: BLE001 - never fail a job over a grade
            report(message=f"Auto grade failed ({type(exc).__name__}); using the preset")
            auto_grade_filter = None

    all_clips: list[dict] = []
    per_profile_cost: dict = {}

    for p_idx, profile in enumerate(profiles):
        span0 = 0.50 + p_idx * (0.45 / len(profiles))
        span = 0.45 / len(profiles)

        # ---- 3. rank (<=3 calls) -----------------------------------------
        report(stage="rank", progress=span0,
               message=f"Ranking for {profile}" + (" +RAW" if raw_addon else ""))
        batches = _batches(cands, MAX_RANK_CALLS)
        ranked: dict[str, dict] = {}
        for i, batch in enumerate(batches, start=1):
            prompt = prompts.rank_prompt(
                profile=profile, raw_addon=raw_addon, shared=fw["SHARED"],
                profile_block=fw.get(profile, ""), exemplars=exemplars,
                candidates=batch, batch_i=i, batch_n=len(batches),
                tuning=tune.load())
            try:
                res = brain.ask_json(prompt, label=f"rank:{profile}:{i}/{len(batches)}",
                                     model=cfg.RANK_MODEL, mode=cfg.BRAIN_MODE,
                                     timeout_s=cfg.BRAIN_TIMEOUT_S, job_id=job_id)
            except brain.BrainError as exc:
                report(message=f"Ranking batch {i} failed ({str(exc)[:90]}) — continuing")
                continue
            for row in (res.get("ranked") or []):
                cid = str(row.get("cid") or "")
                if cid:
                    ranked[cid] = row
            report(stage="rank", progress=span0 + span * 0.35 * i / max(1, len(batches)),
                   message=f"{profile}: ranked {len(ranked)}/{len(cands)}")
            if _cancelled(job_id):
                return _stop(job_id, report, all_clips)

        if not ranked:
            report(message=f"{profile}: ranking produced nothing; skipping this profile")
            continue

        merged = []
        for c in cands:
            r = ranked.get(c["cid"])
            if not r:
                continue
            try:
                score = int(float(r.get("score") or 0))
            except (TypeError, ValueError):
                score = 0
            if score < SCORE_FLOOR:
                continue
            if r.get("complete_thought") is False:
                continue
            merged.append({**c, "score": score, "hook": str(r.get("hook") or "").strip(),
                           "why": str(r.get("why") or "").strip(),
                           "structure": str(r.get("structure") or "").strip(),
                           "topic": str(r.get("topic") or "").strip(),
                           "profile_tag": str(r.get("profile_tag") or profile).upper(),
                           "peak_lines": r.get("peak_lines") or []})
        merged.sort(key=lambda c: -c["score"])
        merged = _diversify(merged, SHORTLIST_POOL)
        if not merged:
            report(message=f"{profile}: nothing scored above {SCORE_FLOOR}")
            continue

        # ---- 3b. shortlist: one harder pass over the survivors ------------
        # The ranking pass scores candidates in isolation, in batches, so no
        # batch ever sees the whole field. This one does — every survivor, one
        # call, judged against each other. It also stops the machine rendering
        # clips it will never post: at 2/day, 21 is ten days of runway, and
        # rendering 60 throws ~39 encodes (about 45 minutes) away.
        if len(merged) > MAX_RENDER_PER_PROFILE:
            report(stage="rank", progress=span0 + span * 0.38,
                   message=f"{profile}: second pass, {len(merged)} -> "
                           f"{MAX_RENDER_PER_PROFILE} best")
            try:
                short = brain.ask_json(
                    prompts.shortlist_prompt(
                        profile=profile, shared=fw["SHARED"],
                        profile_block=fw.get(profile, ""), exemplars=exemplars,
                        tuning=tune.load(), clips=merged,
                        keep=MAX_RENDER_PER_PROFILE),
                    label=f"shortlist:{profile}", model=cfg.RANK_MODEL,
                    mode=cfg.BRAIN_MODE, timeout_s=cfg.BRAIN_TIMEOUT_S, job_id=job_id)
                keep_ids = [str(k.get("cid")) for k in (short.get("keep") or [])]
                why_by_cid = {str(k.get("cid")): str(k.get("why") or "")
                              for k in (short.get("keep") or [])}
                by_cid_all = {c["cid"]: c for c in merged}
                picked = [by_cid_all[c] for c in keep_ids if c in by_cid_all]
                if picked:
                    for c in picked:
                        if why_by_cid.get(c["cid"]):
                            c["shortlist_why"] = why_by_cid[c["cid"]]
                    cuts = short.get("cut_reasons") or []
                    report(message=f"{profile}: kept {len(picked)}"
                                   + (f", e.g. cut {cuts[0].get('cid')} — "
                                      f"{str(cuts[0].get('why'))[:70]}" if cuts else ""))
                    merged = picked[:MAX_RENDER_PER_PROFILE]
                else:
                    # A malformed reply must not silently drop every clip.
                    report(message=f"{profile}: shortlist returned nothing usable; "
                                   f"falling back to top {MAX_RENDER_PER_PROFILE} by score")
                    merged = merged[:MAX_RENDER_PER_PROFILE]
            except brain.BrainError as exc:
                report(message=f"{profile}: shortlist call failed ({str(exc)[:80]}); "
                               f"using top {MAX_RENDER_PER_PROFILE} by score")
                merged = merged[:MAX_RENDER_PER_PROFILE]

        # ---- 4. captions + review (ONE call for the whole video) ---------
        if _cancelled(job_id):
            return _stop(job_id, report, all_clips)
        report(stage="caption", progress=span0 + span * 0.45,
               message=f"{profile}: writing {len(merged)} captions in one call")
        try:
            cap = brain.ask_json(
                prompts.caption_prompt(profile=profile, shared=fw["SHARED"],
                                       profile_block=fw.get(profile, ""),
                                       exemplars=exemplars, clips=merged),
                label=f"caption:{profile}", model=cfg.CAPTION_MODEL,
                mode=cfg.BRAIN_MODE, timeout_s=cfg.BRAIN_TIMEOUT_S, job_id=job_id)
            by_cid = {str(c.get("cid")): c for c in (cap.get("clips") or [])}
        except brain.BrainError as exc:
            report(message=f"{profile}: caption call failed ({str(exc)[:90]}); "
                           "clips still render without copy")
            by_cid = {}

        # ---- 5. render (single pass from source) -------------------------
        outdir = cfg.CLIPS / job_id / profile.lower()
        outdir.mkdir(parents=True, exist_ok=True)
        done_count = [0]
        import threading  # noqa: PLC0415
        lock = threading.Lock()

        def _one(rank_i: int, c: dict) -> dict:
            """Render one clip. Runs on a worker thread."""
            cid = f"{profile.lower()}-{c['cid']}"
            base = outdir / f"{rank_i:02d}_{c['cid']}"
            meta = by_cid.get(c["cid"], {})
            words = segment.words_between(tr, c["start_s"], c["end_s"])

            clip = {
                "id": cid, "rank": rank_i, "profile": profile,
                "profile_tag": c["profile_tag"], "cid": c["cid"],
                "start_s": c["start_s"], "end_s": c["end_s"],
                "duration_s": c["duration_s"], "timestamp": segment.stamp(c["start_s"]),
                "score": c["score"], "why": c["why"], "structure": c["structure"],
                "topic": c.get("topic", ""),
                "peak_lines": c["peak_lines"], "transcript": c["text"],
                "hook": (meta.get("hook") or c["hook"]).strip(),
                "caption": str(meta.get("caption") or "").strip(),
                "hashtags": [str(h) for h in (meta.get("hashtags") or [])][:8],
                "review_verdict": str(meta.get("review_verdict") or "").upper(),
                "review_reason": str(meta.get("review_reason") or "").strip(),
                "audio_ok": c["audio"]["ok"], "audio_note": c["audio"]["note"],
                "audio_flags": c["audio"]["flags"],
                "decision": "pending", "path": None, "path_vertical": None,
            }
            # Lossless pre-cut FIRST. Every downstream step (face detect, silence
            # scan, loudness measure, both renders) then reads an ~80s file rather
            # than seeking around a 10 GB 4K one. Measured before this: 11 minutes
            # per clip because ffmpeg re-decoded from the file start every time.
            seg = outdir / f"{rank_i:02d}_{c['cid']}_src.mp4"
            try:
                render.cut_segment(src, seg, c["start_s"], c["duration_s"])
                work, w_start, w_end = seg, 1.0, 1.0 + c["duration_s"]
            except Exception:  # noqa: BLE001 - fall back to seeking the original
                work, w_start, w_end = src, c["start_s"], c["end_s"]

            # Look and sound. Read fresh per clip so a change made on the phone
            # applies to the next render without restarting the server.
            look = {**_look.load(), **((job or {}).get("look") or {})}
            c_preset = look["color_preset"]
            c_amt = float(look["color_intensity"])
            afilter, ainfo = audio_fx.build_chain(
                work, w_start, c["duration_s"],
                preset=look["audio_preset"], intensity=float(look["audio_intensity"]))
            if auto_grade_filter is not None:
                grade = auto_grade_filter
                clip["grade"] = "Auto — measured from the footage"
            else:
                grade = color.fast_filter_chain(c_preset, c_amt)
                clip["grade"] = color.describe(c_preset, c_amt)

            # Volume and aspect come from the same saved defaults. Before this,
            # "use this style for all" saved a ratio that the batch render never
            # read, so every new clip came out in the source shape.
            vol = float(look.get("volume_db") or 0)
            if abs(vol) > 0.1:
                afilter = f"{afilter},volume={vol:.1f}dB" if afilter else f"volume={vol:.1f}dB"
            crop, delivery = edit.crop_filter(
                {"aspect": {"ratio": look.get("aspect_ratio", "original"),
                            "pan": look.get("aspect_pan", 0.62),
                            "cx": look.get("aspect_panx", 0.5),
                            "zoom": look.get("aspect_zoom", 1.0)}}, work)
            clip["aspect"] = look.get("aspect_ratio", "original")
            # Silence cutting is OFF by default. Even tuned conservatively it
            # clipped words on real speech, and 0.8s saved is not worth a clip
            # that sounds broken. CM_CUT_SILENCE=1 re-enables it.
            keeps = (render.silence_cuts(work, w_start, w_end)
                     if getattr(cfg, "CUT_SILENCE", False) else None)
            trimmed = (c["duration_s"] - sum(b - a for a, b in keeps)) if keeps else 0.0
            punches = c.get("peak_lines") or []
            clip["audio_fx"] = audio_fx.describe(ainfo)
            clip["silence_removed_s"] = round(max(0.0, trimmed), 1)
            clip["punch_ins"] = len(punches[:3])

            # Fade out only when a CTA actually follows. Fading to black at the
            # end of a standalone clip would just look like it broke.
            will_append = bool(look.get("outro", True) and outro.info()["present"])
            fade_out = float(getattr(cfg, "OUTRO_FADE_OUT", 0.5)) if will_append else 0.0

            # Captions burn into the 16:9 - that's the clip that gets posted.
            # The .srt is always written — it is the transcript the editor edits.
            # Whether it gets BURNED here is a setting, off by default.
            srt = render.write_karaoke_srt(words, base.with_suffix(".srt"))
            burn = bool(getattr(cfg, "BURN_CAPTIONS_AT_RENDER", False))
            clip["captions_burned"] = bool(srt) and burn
            try:
                wide = render.render_wide(work, base.with_suffix(".mp4"),
                                          w_start, w_end,
                                          punch_ins=punches, keeps=keeps,
                                          afilter=afilter, subs_path=(srt if burn else None),
                                          grade=grade, fade_out=fade_out,
                                          out_duration=c["duration_s"] - trimmed,
                                          crop=crop, delivery=delivery)
                clip["path"] = str(wide)
            except Exception as exc:  # noqa: BLE001 - one bad clip must not sink the job
                clip["render_error"] = f"{type(exc).__name__}: {exc}"[:300]
                report(message=f"{profile}: 16:9 render failed for {c['cid']} "
                               f"({type(exc).__name__})")

            # ---- CTA outro -------------------------------------------------
            # Spliced on with a stream copy, so the clip keeps exactly the one
            # encode it came out of the renderer with. The outro is baked once
            # per geometry (and per look) and reused by every clip after that.
            if clip["path"] and will_append:
                try:
                    geo = outro.probe(Path(clip["path"]))
                    baked = outro.bake(geo["w"], geo["h"], geo["fps"],
                                       grade=grade, afilter=afilter,
                                       codec_args=render._video_codec_args(),
                                       audio_args=render._audio_args(),
                                       fade_in=float(getattr(cfg, "OUTRO_FADE_IN", 0.5)))
                    if baked:
                        _, how = outro.append(Path(clip["path"]), baked)
                        clip["outro"] = how
                except Exception as exc:  # noqa: BLE001 - a CTA is never worth losing a clip over
                    clip["outro_error"] = f"{type(exc).__name__}: {exc}"[:200]

            if cfg.MAKE_VERTICAL and clip["path"]:
                try:
                    vert = render.render_vertical(
                        work, base.with_name(base.name + "_vertical").with_suffix(".mp4"),
                        w_start, w_end, subs_path=(srt if burn else None),
                        punch_ins=punches, keeps=keeps, afilter=afilter)
                    if vert:
                        clip["path_vertical"] = str(vert)
                        clip["captions_burned"] = bool(srt) and burn
                    else:
                        clip["vertical_note"] = "no faces detected; 9:16 skipped"
                # Broad on purpose: the 16:9 cut is the deliverable, and a face
                # tracking or OpenCV problem must never discard a clip that
                # already rendered.
                except Exception as exc:  # noqa: BLE001
                    clip["vertical_note"] = f"9:16 failed: {type(exc).__name__}: {exc}"[:200]

            if work is not src:
                try:
                    work.unlink(missing_ok=True)   # temp remux, not a deliverable
                except OSError:
                    pass

            with lock:
                all_clips.append(clip)
                done_count[0] += 1
                report(stage="render",
                       progress=span0 + span * (0.5 + 0.5 * done_count[0] / len(merged)),
                       message=f"{profile}: rendered {done_count[0]}/{len(merged)}",
                       clips=all_clips)
            return clip

        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
        report(stage="render", progress=span0 + span * 0.5,
               message=f"{profile}: rendering {len(merged)} clips, "
                       f"{RENDER_WORKERS} at a time")
        with ThreadPoolExecutor(max_workers=RENDER_WORKERS) as pool:
            futures = [pool.submit(_one, i, c) for i, c in enumerate(merged, start=1)]
            for fut in futures:
                if _cancelled(job_id):
                    pool.shutdown(wait=False, cancel_futures=True)
                    return _stop(job_id, report, all_clips)
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 - one bad clip, not the job
                    report(message=f"{profile}: a clip failed ({type(exc).__name__})")

        per_profile_cost[profile] = brain.usage_summary(job_id)

    if not all_clips:
        raise RuntimeError("ranking and rendering produced no clips")

    usage = brain.usage_summary(job_id)
    report(stage="done", progress=1.0,
           message=f"{len(all_clips)} clips ready ({usage['calls']} brain calls, "
                   f"${usage['cost_usd']:.2f} notional)",
           clips=all_clips, brain_usage=usage, brain_by_profile=per_profile_cost)
