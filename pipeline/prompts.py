"""Prompt construction. Kept apart from the call plumbing so tuning the machine's
taste is a text edit, not a code change."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import segment

PROFILES = ("BRAND", "TRAINER", "RAW")


def load_frameworks(path: Path) -> dict:
    """Split FRAMEWORKS.md into its SHARED block and per-profile blocks."""
    if not path.exists():
        return {"SHARED": "", **{p: "" for p in PROFILES}}
    text = path.read_text(encoding="utf-8", errors="replace")
    out = {"SHARED": "", **{p: "" for p in PROFILES}}
    chunks = re.split(r"^##\s+", text, flags=re.MULTILINE)
    for chunk in chunks:
        head = chunk.split("\n", 1)[0].strip().upper()
        body = chunk.split("\n", 1)[1] if "\n" in chunk else ""
        if head.startswith("SHARED"):
            out["SHARED"] = body.strip()
        elif head.startswith("PROFILE:"):
            name = head.split(":", 1)[1].strip()
            if name in out:
                out[name] = body.strip()
    return out


def load_exemplars(path: Path, limit_chars: int = 3000) -> str:
    """Positive taste (Upgrade Pass 2). Absent until Jason drops exemplars in."""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit_chars].strip()


def rank_prompt(*, profile: str, raw_addon: bool, shared: str, profile_block: str,
                exemplars: str, candidates: list[dict], batch_i: int, batch_n: int,
                tuning: str = "") -> str:
    lines = [
        f"You are ranking candidate short-form clips from one long recording for the "
        f"{profile} profile. This is batch {batch_i} of {batch_n}.",
        "",
        "== SHARED RULES ==", shared, "",
        f"== PROFILE: {profile} ==", profile_block, "",
    ]
    if raw_addon:
        lines += ["== ALSO ACCEPT: RAW ==",
                  "In addition to the profile above, also surface authentic unpolished "
                  "moments (banter, laughing, done>perfect energy, 15-45s). Tag those "
                  "with \"profile_tag\":\"RAW\". Everything else gets the main profile tag.", ""]
    if exemplars:
        lines += ["== PATTERNS JASON LOVES (weight these heavily) ==", exemplars, ""]
    # Learned from real rejections. Placed AFTER the hand-written rubric so it
    # reads as an amendment to it, never a replacement for it.
    if tuning:
        lines += ["== LEARNED FROM PAST REJECTIONS (amends the rules above) ==",
                  tuning, ""]

    lines += [
        "== CANDIDATES ==",
        "Each candidate is a complete-thought window with its transcript.",
        "",
    ]
    for c in candidates:
        lines.append(
            f"[{c['cid']}] {segment.stamp(c['start_s'])}-{segment.stamp(c['end_s'])} "
            f"({c['duration_s']:.0f}s){' [AHA-CUE DETECTED]' if c.get('has_aha') else ''}\n"
            f"{c['text']}\n")

    lines += [
        "== TASK ==",
        "Score EVERY candidate 0-100 against the rules above. Reward the profile's "
        "top-ranked pattern explicitly.",
        "",
        "VOLUME AND VARIETY MATTER. This is an hour-plus recording and the goal is "
        "a deep bench of posts, not a shortlist. Do NOT be stingy:",
        "- Score generously anything genuinely usable. 45+ means renderable.",
        "- Reward DIFFERENT KINDS of value: teaching, a story, a hot take, a "
        "definition, a myth corrected, a client example, a one-liner, a reframe.",
        "- Overlap and repetition are FINE. The same idea told two ways is two "
        "posts. Never downrank something for resembling another candidate.",
        "- Vary the length. Short punchy 30s clips and longer 2-minute teaches are "
        "both wanted; do not push everything to the maximum length.",
        "- SAME MEAT, DIFFERENT CUTS is the goal. One good idea sliced three ways "
        "with three different hooks is three posts, not one. Score each cut on "
        "its own merit and give each a DIFFERENT hook - do not reuse a hook "
        "across overlapping candidates.",
        "- Aim to pass 40+ candidates from an hour of material. Quantity with "
        "quality: the bench should be deep enough to post daily for weeks.",
        "- BUT SPREAD IT OUT. Repeating one good idea is fine; a feed that is ALL "
        "one idea is not. Pull from across the WHOLE recording, not just the "
        "strongest stretch, and cover different subjects, not one subject "
        "restated forty times.",
        "- Give each clip a short \"topic\" tag (2-4 words, e.g. \"follow-up\", "
        "\"pricing\", \"dirty bulk story\") so near-duplicates are visible.",
        "",
        "Reply with ONLY this JSON, no prose:",
        '{"ranked":[{"cid":"c001","score":87,"profile_tag":"BRAND",'
        '"hook":"the scroll-stopping first line, <=12 words",'
        '"why":"one line: which framework rule it hits",'
        '"structure":"attack-relate-resolve|loop|teach-aha|story|raw|other",'
        '"topic":"2-4 words naming the subject",'
        '"peak_lines":[{"t_rel_s":12.5,"why":"the aha"}],'
        '"complete_thought":true,"self_contained":true}]}',
        "",
        "Rules for your output:",
        "- Include every candidate id you were given, even low scorers.",
        "- score <55 means do not post.",
        "- peak_lines: 1-3 moments, seconds RELATIVE to the clip start, where the "
        "emphasis lands (the aha, the attack line). Used for punch-in edits.",
        "- Set complete_thought=false if it starts or ends mid-sentence.",
    ]
    return "\n".join(lines)


def caption_prompt(*, profile: str, shared: str, profile_block: str, exemplars: str,
                   clips: list[dict]) -> str:
    """ONE call for every clip in the video (efficiency pass): per-clip calls paid
    the ~22.7k fixed harness context N times over for no quality gain."""
    lines = [
        f"Write posting copy for {len(clips)} short clips from one recording, "
        f"{profile} profile. Also act as a second reviewer on each.",
        "",
        "== SHARED RULES ==", shared, "",
        f"== PROFILE: {profile} ==", profile_block, "",
    ]
    if exemplars:
        lines += ["== PATTERNS JASON LOVES ==", exemplars, ""]
    lines += ["== CLIPS ==", ""]
    for c in clips:
        lines.append(f"[{c['cid']}] {c['duration_s']:.0f}s "
                     f"(tag {c.get('profile_tag') or profile})\n{c['text']}\n")
    lines += [
        "== TASK ==",
        "For each clip return hook, caption, hashtags, and a review verdict.",
        "The caption voice is defined in the profile block above - follow it exactly. "
        "No corporate tone, no hype, no emoji spam.",
        "",
        "Reply with ONLY this JSON:",
        '{"clips":[{"cid":"c001",'
        '"hook":"<=12 words, the on-screen/first line",'
        '"caption":"2-4 sentences in the profile voice",'
        '"hashtags":["#five","#to","#eight","#niche","#tags"],'
        '"review_verdict":"PASS",'
        '"review_reason":"one line; required when FLAG"}]}',
        "",
        "FLAG (not PASS) when: it cuts mid-thought, the energy is flat, it needs "
        "context the viewer doesn't have, or the profile's hard filter is broken "
        "(e.g. business talk in a TRAINER clip).",
        "Hashtags: 5-8, niche and specific. No #fyp, no #viral, no #explore.",
    ]
    return "\n".join(lines)


def vision_prompt(*, clips_frames: list[dict]) -> str:
    """Upgrade Pass 2 visual QC. Batched across clips because vision calls are the
    expensive kind."""
    lines = [
        "You are doing visual quality control on rendered short clips. For each "
        "clip you get keyframes sampled across it plus its transcript.",
        "",
    ]
    for c in clips_frames:
        lines.append(f"[{c['cid']}] frames: {len(c['frames'])}\n"
                     f"transcript: {c['text'][:400]}\n")
    lines += [
        "== TASK ==",
        "Judge framing, lighting, and whether the shot goes visually dead.",
        "",
        "Reply with ONLY this JSON:",
        '{"clips":[{"cid":"c001","framing_ok":true,"lighting_ok":true,'
        '"faces_expected":2,"faces_seen":2,"dead_visual_stretch":false,'
        '"visual_verdict":"PASS","note":"one line"}]}',
        "",
        "framing_ok=false when a speaker is cut off, out of frame, or badly "
        "off-centre. If two people are talking in the transcript but only one is "
        "visible across the frames, say so in the note.",
        "dead_visual_stretch=true when it's a static wide shot with no change for "
        "most of the clip (>15s of visual sameness).",
        "visual_verdict is PASS or FLAG.",
    ]
    return "\n".join(lines)


def shortlist_prompt(*, profile: str, shared: str, profile_block: str,
                     exemplars: str, tuning: str, clips: list[dict],
                     keep: int) -> str:
    """A second, HARDER pass over the clips that already won the first round.

    Why this exists: the first pass scores every candidate in isolation, in
    batches, against a rubric. That is good at "is this postable" and bad at
    "is this better than the other 59", because no batch ever sees the whole
    field. This pass does — one call, every survivor, ranked against each other.

    It also stops the machine rendering clips it will never post. At two a day,
    21 clips is ten days of runway; rendering 60 means ~39 encodes, roughly 45
    minutes, thrown away.
    """
    lines = [
        f"You already picked {len(clips)} clips for the {profile} profile out of a "
        f"long recording. Now cut them down to the {keep} BEST.",
        "",
        "This is a harder bar than the first pass. There, the question was 'is this "
        "postable'. Here it is 'would I be embarrassed if this went out instead of "
        "one of the others'. You can see the whole field at once, which the first "
        "pass could not.",
        "",
        "== THE RULES THEY WERE PICKED AGAINST ==", shared, "",
        f"== PROFILE: {profile} ==", profile_block, "",
    ]
    if exemplars:
        lines += ["== WHAT HE LOVES ==", exemplars[:2000], ""]
    if tuning:
        lines += ["== LEARNED FROM PAST REJECTIONS ==", tuning, ""]

    lines += ["== THE CANDIDATES ==", ""]
    for c in clips:
        lines.append(
            f"[{c['cid']}] score {c.get('score')} · {c.get('structure','?')} · "
            f"{c.get('topic','?')} · {c.get('duration_s',0):.0f}s\n"
            f"HOOK: {c.get('hook','')}\n"
            f"{(c.get('text') or '')[:700]}\n")

    lines += [
        "== HOW TO CHOOSE ==",
        f"- Keep exactly {keep}, or fewer if fewer genuinely clear the bar. Never pad.",
        "- Cut near-duplicates. Same idea told twice = keep the better telling only.",
        "- Cut anything that opens on a pronoun or 'this' with no antecedent — a "
        "  stranger scrolling has no context.",
        "- Cut anything that needs the rest of the conversation to make sense.",
        "- Prefer a complete thought over a strong opening that trails off.",
        "- SPREAD: do not take everything from one stretch of the recording or one "
        "  topic. A week of posts should not sound like one meeting.",
        "- Shorter is usually better. If two are equal, keep the tighter one.",
        "",
        "Reply with ONLY this JSON, no prose:",
        '{"keep":[{"cid":"c001","rank":1,"why":"one line: why this beat the others"}],'
        '"cut_reasons":[{"cid":"c009","why":"near-duplicate of c001, weaker open"}]}',
        "",
        "- `keep` ordered best first.",
        "- `cut_reasons` only for the notable cuts, not all of them. This is what "
        "  gets reviewed when the shortlist looks wrong.",
    ]
    return "\n".join(lines)
