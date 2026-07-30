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

from . import audio_gate, brain, prompts, render, segment, transcribe

MAX_RANK_CALLS = 3
# A 67-minute recording yielded 4 clips at a cap of 8 and a floor of 55 — far too
# stingy for a source that long. Volume with variety is the goal; overlap and
# repetition are acceptable because the same idea told two ways are two posts.
MAX_RENDER_PER_PROFILE = 40
SCORE_FLOOR = 45


def _profiles_for(job: dict) -> tuple[list[str], bool]:
    want = str(job.get("profile") or "BRAND").upper()
    raw = bool(job.get("include_raw"))
    if want == "BOTH":
        return ["BRAND", "TRAINER"], raw
    if want in prompts.PROFILES:
        return [want], raw
    return ["BRAND"], raw


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
                                     max_candidates=200, overlap_frac=0.85)
    if not cands:
        raise RuntimeError("no complete-thought windows of the right length were found")

    for c in cands:
        c["audio"] = audio_gate.analyse(src, c["start_s"], c["end_s"])
    flagged = sum(1 for c in cands if not c["audio"]["ok"])
    report(stage="candidates", progress=0.50,
           message=f"{len(cands)} candidates ({flagged} with audio warnings)",
           candidate_count=len(cands))

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
                candidates=batch, batch_i=i, batch_n=len(batches))
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
                           "profile_tag": str(r.get("profile_tag") or profile).upper(),
                           "peak_lines": r.get("peak_lines") or []})
        merged.sort(key=lambda c: -c["score"])
        merged = merged[:MAX_RENDER_PER_PROFILE]
        if not merged:
            report(message=f"{profile}: nothing scored above {SCORE_FLOOR}")
            continue

        # ---- 4. captions + review (ONE call for the whole video) ---------
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
        for rank_i, c in enumerate(merged, start=1):
            frac = span0 + span * (0.5 + 0.5 * rank_i / len(merged))
            report(stage="render", progress=frac,
                   message=f"{profile}: rendering {rank_i}/{len(merged)} "
                           f"({segment.stamp(c['start_s'])})")
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
            try:
                wide = render.render_wide(src, base.with_suffix(".mp4"),
                                          c["start_s"], c["end_s"])
                clip["path"] = str(wide)
            except Exception as exc:  # noqa: BLE001 - one bad clip must not sink the job
                clip["render_error"] = f"{type(exc).__name__}: {exc}"[:300]
                report(message=f"{profile}: 16:9 render failed for {c['cid']} "
                               f"({type(exc).__name__})")

            if cfg.MAKE_VERTICAL and clip["path"]:
                try:
                    srt = render.write_karaoke_srt(words, base.with_suffix(".srt"))
                    vert = render.render_vertical(
                        src, base.with_name(base.name + "_vertical").with_suffix(".mp4"),
                        c["start_s"], c["end_s"], subs_path=srt)
                    if vert:
                        clip["path_vertical"] = str(vert)
                        clip["captions_burned"] = bool(srt)
                    else:
                        clip["vertical_note"] = "no faces detected; 9:16 skipped"
                # Broad on purpose: the 16:9 cut is the deliverable, and a face
                # tracking or OpenCV problem must never discard a clip that
                # already rendered.
                except Exception as exc:  # noqa: BLE001
                    clip["vertical_note"] = f"9:16 failed: {type(exc).__name__}: {exc}"[:200]

            all_clips.append(clip)
            from server import jobs as _jobs  # noqa: PLC0415 - avoid import cycle
            _jobs.update(job_id, clips=all_clips)

        per_profile_cost[profile] = brain.usage_summary(job_id)

    if not all_clips:
        raise RuntimeError("ranking and rendering produced no clips")

    usage = brain.usage_summary(job_id)
    report(stage="done", progress=1.0,
           message=f"{len(all_clips)} clips ready ({usage['calls']} brain calls, "
                   f"${usage['cost_usd']:.2f} notional)",
           clips=all_clips, brain_usage=usage, brain_by_profile=per_profile_cost)
