"""Transcript -> candidate clip windows.

The brain is expensive per call, so it must never be handed 900 raw whisper
segments. This narrows an hour of talk to a few dozen defensible candidates using
only local rules, then the ranker judges those.

The one rule that outranks everything: a candidate must start and end on a
complete thought. Whisper segments already break on speech pauses, so windows are
built by joining whole segments and only closing on sentence-final punctuation.
"""
from __future__ import annotations

import re
from typing import Optional

# Openers that prove a clip depends on something the viewer never heard.
_DEPENDENT_START = re.compile(
    r"^(and|but|so|because|then|which|that's why|like i said|as i mentioned|"
    r"going back|anyway|also|plus|or |too\b|again\b)",
    re.IGNORECASE)
_SENTENCE_END = re.compile(r'[.!?]["\')\]]?\s*$')
# The teach->aha signal BRAND ranks highest. Kept here so a candidate carrying one
# is never dropped by a length heuristic before the ranker sees it.
AHA_CUES = [
    # Verbatim from Jason's own recordings - these are the exact phrases his
    # trainers use at the moment they get it.
    "makes perfect sense", "sparked my brain", "say that again", "writing that down",
    "i appreciate you", "bro", "hold on", "wait so", "that makes sense",
    "i never thought", "never thought about it", "oh okay", "ohhh", "that's smart",
    "gotcha", "that clicks", "makes so much sense", "okay okay",
    "that's a good point", "i see what you", "that's actually",
]

# RECITAL is the strongest signal there is: the trainer repeating the framework
# back in his own words ("so you're not posting content, you're building a case").
# Reward it above every other cue - agreement is cheap, restatement is proof the
# idea landed.
RECITAL_MARKERS = [
    "so you're saying", "so basically", "so it's not", "so instead of",
    "so what you're saying", "so i'm not", "so you're not", "so if i",
    "oh so", "so like", "so the point is", "so really",
]


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip()


def has_aha(text: str) -> bool:
    low = text.lower()
    return any(c in low for c in AHA_CUES) or has_recital(text)


def has_recital(text: str) -> bool:
    """Did the other person restate the idea in their own words? Jason's #1 signal."""
    low = text.lower()
    return any(m in low for m in RECITAL_MARKERS)


def _complete_thought(text: str) -> bool:
    return bool(_SENTENCE_END.search(text.strip()))


def _self_contained(text: str) -> bool:
    return not _DEPENDENT_START.match(text.strip())


def build_candidates(transcript: dict, *, min_s: float = 30.0, max_s: float = 80.0,
                     stride: int = 1, max_candidates: int = 200,
                     overlap_frac: float = 0.85) -> list[dict]:
    """Sliding windows over whole segments, kept only if they're a complete,
    self-contained thought of the right length."""
    segs = [s for s in transcript.get("segments", []) if _clean(s.get("text"))]
    out: list[dict] = []
    n = len(segs)

    for i in range(0, n, stride):
        if not _self_contained(segs[i]["text"]):
            continue
        text_parts: list[str] = []
        for j in range(i, n):
            text_parts.append(_clean(segs[j]["text"]))
            start = float(segs[i]["start"])
            end = float(segs[j]["end"])
            dur = end - start
            if dur < min_s:
                continue
            if dur > max_s:
                break
            joined = " ".join(text_parts)
            if not _complete_thought(joined):
                continue
            out.append({
                "start_s": round(start, 2),
                "end_s": round(end, 2),
                "duration_s": round(dur, 2),
                "text": joined,
                "seg_from": i,
                "seg_to": j,
                "word_count": len(joined.split()),
                "has_aha": has_aha(joined),
                "has_recital": has_recital(joined),
            })
    if not out:
        return []

    # Overlap suppression, aha-first. Deliberately loose (0.85): a near-duplicate
    # window is a different edit of the same moment, and two edits of one good
    # moment are two usable posts. Only near-identical spans get dropped.
    # Length is NOT a tiebreaker any more — sorting by longest made every clip
    # come out at exactly the maximum.
    # Recital first, then any aha cue, then chronological. A window where the
    # framework got repeated back survives dedup ahead of everything else.
    out.sort(key=lambda c: (not c["has_recital"], not c["has_aha"], c["start_s"]))
    kept: list[dict] = []
    for cand in out:
        clash = False
        for k in kept:
            overlap = min(cand["end_s"], k["end_s"]) - max(cand["start_s"], k["start_s"])
            if overlap > overlap_frac * min(cand["duration_s"], k["duration_s"]):
                clash = True
                break
        if not clash:
            kept.append(cand)
        if len(kept) >= max_candidates:
            break

    kept.sort(key=lambda c: c["start_s"])
    for idx, c in enumerate(kept):
        c["cid"] = f"c{idx:03d}"
    return kept


def stamp(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 \
        else f"{s // 60:d}:{s % 60:02d}"


def words_between(transcript: dict, start_s: float, end_s: float) -> list[dict]:
    """Word timings inside a window, rebased to clip-relative time. Feeds karaoke
    captions and punch-in placement."""
    out = []
    for seg in transcript.get("segments", []):
        if float(seg["end"]) < start_s or float(seg["start"]) > end_s:
            continue
        for w in seg.get("words") or []:
            ws, we = float(w["start"]), float(w["end"])
            if we < start_s or ws > end_s:
                continue
            out.append({"w": w["w"].strip(),
                        "start": round(max(0.0, ws - start_s), 3),
                        "end": round(max(0.0, we - start_s), 3)})
    return out
