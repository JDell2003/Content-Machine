"""`tune` — turn accumulated taste signal into rubric amendments.

The machine already collects three kinds of signal:

    feedback.jsonl    every rejection, with a required reason and optional note
    EXEMPLARS.md      clips and hooks you explicitly loved
    performance.jsonl what actually performed once posted (Upgrade Pass 3)

None of it changes the ranking on its own. `tune` reads all three, finds the
patterns, and writes TUNING.md — which rank_prompt injects on every future run.
So taste compounds instead of being re-explained every session.

TWO DELIBERATE REFUSALS
-----------------------
1. It will not run on thin data. Under MIN_REJECTIONS the patterns are noise,
   and a confident-sounding rubric built from four rejections is worse than no
   rubric, because it silently biases every future run. It says so and stops.

2. It never edits FRAMEWORKS.md. That file is your hand-written rubric; a model
   rewriting it would slowly launder your judgement into its own. TUNING.md is a
   separate, additive file you can read in one sitting and delete if it drifts.

Cost: ONE brain call, and only when there is enough signal to justify it.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
FEEDBACK = ROOT / "feedback.jsonl"
PERFORMANCE = ROOT / "performance.jsonl"
EXEMPLARS = ROOT / "EXEMPLARS.md"
TUNING = ROOT / "TUNING.md"

MIN_REJECTIONS = 8


def _read_jsonl(path: Path, limit: int = 400) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def signal() -> dict:
    """What we know right now, with no brain call. Drives the UI readout."""
    rej = _read_jsonl(FEEDBACK)
    perf = _read_jsonl(PERFORMANCE)
    by = lambda key: Counter(str(r.get(key) or "?") for r in rej).most_common(8)  # noqa: E731
    return {
        "rejections": len(rej),
        "needed": max(0, MIN_REJECTIONS - len(rej)),
        "ready": len(rej) >= MIN_REJECTIONS,
        "by_reason": by("reason"),
        "by_profile": by("profile"),
        "by_structure": by("structure"),
        "by_topic": by("topic"),
        "posts_tracked": len(perf),
        "has_exemplars": EXEMPLARS.exists(),
        "tuned_at": TUNING.stat().st_mtime if TUNING.exists() else None,
    }


def _prompt(sig: dict, rej: list[dict], perf: list[dict], exemplars: str) -> str:
    def notes(reason: str, n: int = 6) -> list[str]:
        return [str(r.get("note") or "").strip() for r in rej
                if r.get("reason") == reason and str(r.get("note") or "").strip()][:n]

    lines = [
        "You are tuning the ranking rubric for a short-form clip machine.",
        "",
        "Below is real reviewer behaviour: every clip the operator REJECTED, with "
        "the reason they picked, plus what they explicitly liked.",
        "",
        f"Total rejections: {sig['rejections']}",
        "Rejections by reason: " + ", ".join(f"{k}={v}" for k, v in sig["by_reason"]),
        "Rejections by profile: " + ", ".join(f"{k}={v}" for k, v in sig["by_profile"]),
        "Rejections by structure: " + ", ".join(f"{k}={v}" for k, v in sig["by_structure"]),
        "Rejections by topic: " + ", ".join(f"{k}={v}" for k, v in sig["by_topic"]),
        "",
    ]
    for reason, _ in sig["by_reason"]:
        ns = notes(reason)
        if ns:
            lines.append(f'Operator notes on "{reason}":')
            lines += [f"  - {n[:200]}" for n in ns]
    # A rejected clip's own hook is the most concrete signal there is.
    hooks = [str(r.get("hook") or "").strip() for r in rej if r.get("hook")][:20]
    if hooks:
        lines += ["", "Hooks that were rejected:"] + [f"  - {h[:120]}" for h in hooks]
    if exemplars:
        lines += ["", "== WHAT THEY LOVE (EXEMPLARS.md) ==", exemplars[:2500]]
    if perf:
        lines += ["", "== POSTED PERFORMANCE =="]
        for row in perf[-25:]:
            lines.append(f"  - {str(row.get('hook') or '')[:90]} :: "
                         f"views={row.get('views','?')} saves={row.get('saves','?')}")

    lines += [
        "",
        "Write rubric AMENDMENTS: concrete, checkable rules that would have caught "
        "these rejections BEFORE rendering.",
        "",
        "Hard requirements:",
        "- Only claim a pattern you can point at in the data above. If the data is "
        "  thin on something, say so instead of inventing a rule.",
        "- Rules must be checkable against a transcript segment, not vibes. "
        '  "Reject clips that open on a pronoun with no antecedent" is checkable; '
        '  "prefer engaging clips" is not.',
        "- Do not restate the existing rubric. Only what should CHANGE.",
        "- Never propose a rule that would have the machine claim revenue, closed "
        "  clients, or paid-ads results. Those claims are forbidden regardless of data.",
        "",
        'Return JSON: {"amendments": [{"rule": "...", "why": "...", '
        '"evidence": "which rejections support this"}], "confidence": "high|medium|low", '
        '"not_enough_data_on": ["..."]}',
    ]
    return "\n".join(lines)


def run(force: bool = False, ask=None) -> dict:
    """-> {ok, written?, reason?, amendments?}. `ask` is injected for testing."""
    sig = signal()
    if not sig["ready"] and not force:
        return {"ok": False,
                "reason": f"Only {sig['rejections']} rejections logged. "
                          f"{sig['needed']} more and the patterns mean something. "
                          f"Reject clips with reasons as you review — that's the input.",
                "signal": sig}

    rej = _read_jsonl(FEEDBACK)
    perf = _read_jsonl(PERFORMANCE)
    exemplars = (EXEMPLARS.read_text(encoding="utf-8", errors="replace")
                 if EXEMPLARS.exists() else "")

    if ask is None:
        from . import brain  # noqa: PLC0415
        ask = brain.ask_json
    try:
        out = ask(_prompt(sig, rej, perf, exemplars), model="opus", tag="tune")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"brain call failed: {exc}"[:250], "signal": sig}

    amendments = (out or {}).get("amendments") or []
    if not amendments:
        return {"ok": False, "reason": "no amendments proposed", "signal": sig}

    import time  # noqa: PLC0415
    body = [
        "# TUNING",
        "",
        "Generated by `tune` from real rejections. Injected into every ranking run.",
        "This file is ADDITIVE — FRAMEWORKS.md is yours and is never edited here.",
        "Delete this file to go back to the hand-written rubric alone.",
        "",
        f"_Built {time.strftime('%Y-%m-%d %H:%M')} from {sig['rejections']} rejections"
        f"{f', {len(perf)} tracked posts' if perf else ''}. "
        f"Confidence: {(out or {}).get('confidence', 'unknown')}._",
        "",
        "## Amendments",
        "",
    ]
    for i, a in enumerate(amendments, 1):
        body.append(f"{i}. **{str(a.get('rule') or '').strip()}**")
        if a.get("why"):
            body.append(f"   - why: {str(a['why']).strip()}")
        if a.get("evidence"):
            body.append(f"   - from: {str(a['evidence']).strip()}")
        body.append("")
    thin = (out or {}).get("not_enough_data_on") or []
    if thin:
        body += ["## Not enough data yet", ""] + [f"- {t}" for t in thin] + [""]

    TUNING.write_text("\n".join(body), encoding="utf-8")
    return {"ok": True, "written": str(TUNING), "count": len(amendments),
            "confidence": (out or {}).get("confidence"), "signal": sig}


def load(limit_chars: int = 2000) -> str:
    """Injected into rank_prompt. Empty until `tune` has been run."""
    if not TUNING.exists():
        return ""
    return TUNING.read_text(encoding="utf-8", errors="replace")[:limit_chars].strip()
