"""Talk to the machine.

Every `claude -p` call is born blank: it knows only what this module hands it.
That's a superpower and a budget at the same time — the whole mind is curated per
call, so the job here is a lean briefing pack, not a document dump.

Tiering (measured against a naive "ship everything" pack of 30k+ tokens):
    always      MACHINE-VOICE.md + BRAND-CORE.md + recent turns
    on demand   FRAMEWORKS.md   when the message touches ranking/style/editing
                ranked list     when the message is about a job or clips
                feedback tail   when the message is about tuning/rejections
The selector is a keyword/intent check in Python. Spending a brain call to decide
what to put in a brain call would defeat the point.

Threads live on disk as JSON. Past ~15 turns the old ones collapse into a cached
5-line digest so a long thread stops growing linearly.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from . import brain

ROOT = Path(__file__).resolve().parent.parent
THREADS = ROOT / "data" / "threads"
THREADS.mkdir(parents=True, exist_ok=True)

VOICE = ROOT / "MACHINE-VOICE.md"
CORE = ROOT / "BRAND-CORE.md"
FRAMEWORKS = ROOT / "FRAMEWORKS.md"
EXEMPLARS = ROOT / "EXEMPLARS.md"
FEEDBACK = ROOT / "feedback.jsonl"

KEEP_TURNS = 6
COMPACT_AFTER = 15

# Rough but honest: ~4 chars/token for English prose.
def est_tokens(text: str) -> int:
    return max(0, len(text) // 4)


_RULES_WORDS = re.compile(
    r"\b(rank|ranking|score|scoring|framework|rule|taste|style|voice|caption|"
    r"hook|hashtag|profile|brand|trainer|raw|punch[- ]?in|zoom|caption|edit|"
    r"editing|length|clip|silence|crop|fram(e|ing)|prefer|instead|stop|start)\b", re.I)
_JOB_WORDS = re.compile(r"\b(job|clip|ranked|list|verdict|flag|render|last run|batch)\b", re.I)
_TUNE_WORDS = re.compile(r"\b(reject|rejected|tune|feedback|why did|keeps? (doing|picking)|wrong)\b", re.I)


def _read(p: Path, limit: int = 12000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit].strip()
    except OSError:
        return ""


def _latest_job() -> Optional[dict]:
    jobs_dir = ROOT / "data" / "jobs"
    best = None
    for p in jobs_dir.glob("*.json"):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if j.get("clips") and (not best or j.get("created_at", 0) > best.get("created_at", 0)):
            best = j
    return best


def _ranked_summary(job: dict, limit: int = 12) -> str:
    lines = [f"Most recent job: {job.get('original_name')} "
             f"({job.get('profile')}, {len(job.get('clips', []))} clips)"]
    for c in (job.get("clips") or [])[:limit]:
        lines.append(
            f"  #{c.get('rank')} {c.get('timestamp')} {c.get('duration_s', 0):.0f}s "
            f"score={c.get('score')} {c.get('structure', '')} "
            f"verdict={c.get('review_verdict', '-')} decision={c.get('decision', '-')}\n"
            f"     hook: {str(c.get('hook', ''))[:80]}")
    return "\n".join(lines)


def _feedback_tail(n: int = 20) -> str:
    if not FEEDBACK.exists():
        return ""
    try:
        lines = FEEDBACK.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return ""
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        out.append(f"  rejected [{r.get('reason')}] score={r.get('score')} "
                   f"{r.get('structure', '')}: {str(r.get('hook', ''))[:70]}"
                   + (f" — {r['note'][:80]}" if r.get("note") else ""))
    return "\n".join(out)


# ------------------------------------------------------------------ threads
def new_thread(title: str = "") -> dict:
    t = {"id": uuid.uuid4().hex[:10], "title": title or "New thread",
         "created_at": time.time(), "updated_at": time.time(),
         "turns": [], "digest": "", "tokens_last": 0}
    save_thread(t)
    return t


def save_thread(t: dict) -> None:
    t["updated_at"] = time.time()
    p = THREADS / f"{t['id']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(t, indent=2), encoding="utf-8")
    tmp.replace(p)


def load_thread(tid: str) -> Optional[dict]:
    p = THREADS / f"{tid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_threads() -> list[dict]:
    out = []
    for p in THREADS.glob("*.json"):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": t["id"], "title": t.get("title"), "turns": len(t.get("turns", [])),
                    "updated_at": t.get("updated_at"), "tokens_last": t.get("tokens_last", 0)})
    out.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return out


def _compact(thread: dict, cfg) -> None:
    """Collapse old turns into a digest so long threads stop growing linearly."""
    turns = thread.get("turns", [])
    if len(turns) <= COMPACT_AFTER:
        return
    old = turns[:-KEEP_TURNS]
    convo = "\n".join(f"{t['role']}: {t['text'][:600]}" for t in old)
    prompt = ("Summarise this conversation into at most 5 short lines. Keep only "
              "decisions made, preferences stated, and open questions. Drop "
              "pleasantries and anything already applied to a file.\n\n"
              f"{thread.get('digest', '')}\n\n{convo}\n\n"
              'Reply as JSON: {"digest":"line1\\nline2..."}')
    try:
        res = brain.ask_json(prompt, label="chat:compact", model=cfg.CAPTION_MODEL,
                             mode=cfg.BRAIN_MODE, timeout_s=300)
        thread["digest"] = str(res.get("digest") or "")[:1200]
        thread["turns"] = turns[-KEEP_TURNS:]
    except brain.BrainError:
        # Compaction failing must not block the conversation; just trim.
        thread["turns"] = turns[-KEEP_TURNS:]


# ------------------------------------------------------------------ context
def assemble(message: str, thread: dict) -> tuple[str, dict]:
    parts: list[str] = []
    loaded: list[str] = []

    voice = _read(VOICE, 4000)
    if voice:
        parts.append("== HOW YOU TALK ==\n" + voice)
        loaded.append("voice")
    core = _read(CORE, 4000)
    if core:
        parts.append("== WHO YOU WORK FOR ==\n" + core)
        loaded.append("core")

    wants_rules = bool(_RULES_WORDS.search(message))
    wants_job = bool(_JOB_WORDS.search(message))
    wants_tune = bool(_TUNE_WORDS.search(message))

    if wants_rules:
        fw = _read(FRAMEWORKS, 14000)
        if fw:
            parts.append("== FRAMEWORKS.md (the constitution) ==\n" + fw)
            loaded.append("frameworks")
        ex = _read(EXEMPLARS, 4000)
        if ex:
            parts.append("== EXEMPLARS.md ==\n" + ex)
            loaded.append("exemplars")
    if wants_job:
        job = _latest_job()
        if job:
            parts.append("== LATEST RUN ==\n" + _ranked_summary(job))
            loaded.append("ranked-list")
    if wants_tune:
        tail = _feedback_tail()
        if tail:
            parts.append("== RECENT REJECTIONS ==\n" + tail)
            loaded.append("feedback")

    if thread.get("digest"):
        parts.append("== EARLIER IN THIS THREAD ==\n" + thread["digest"])
        loaded.append("digest")
    turns = thread.get("turns", [])[-KEEP_TURNS:]
    if turns:
        parts.append("== RECENT TURNS ==\n" +
                     "\n".join(f"{t['role']}: {t['text']}" for t in turns))
        loaded.append(f"{len(turns)} turns")

    parts.append(
        "== TASK ==\n"
        "Reply to Jason's message below.\n\n"
        "If the message implies a change to taste, ranking, style, caption voice, "
        "or editing rules, END your reply with a proposed diff. Otherwise do NOT "
        "invent one.\n\n"
        "Reply with ONLY this JSON:\n"
        '{"reply":"your answer, few lines, no filler",'
        '"diff":{"file":"FRAMEWORKS.md","find":"exact existing text to replace",'
        '"replace":"the new text","why":"one line"}}\n\n'
        'Omit "diff" entirely when no rule change is implied. "find" must be text '
        "that appears verbatim in the file you were shown, or the diff cannot be "
        "applied.")
    parts.append("== JASON ==\n" + message)

    pack = "\n\n".join(parts)
    return pack, {"loaded": loaded, "tokens": est_tokens(pack)}


# ------------------------------------------------------------------ send
def send(thread: dict, message: str, cfg) -> dict:
    _compact(thread, cfg)
    pack, meta = assemble(message, thread)
    res = brain.ask_json(pack, label="chat", model=getattr(cfg, "CHAT_MODEL", cfg.CAPTION_MODEL),
                         mode=cfg.BRAIN_MODE, timeout_s=cfg.BRAIN_TIMEOUT_S)

    reply = str(res.get("reply") or "").strip() or "(no reply)"
    diff = res.get("diff") if isinstance(res.get("diff"), dict) else None
    if diff and not str(diff.get("find") or "").strip():
        diff = None  # unapplyable; drop rather than show a button that can't work

    thread["turns"].append({"role": "jason", "text": message, "ts": time.time()})
    turn = {"role": "machine", "text": reply, "ts": time.time(),
            "context": meta["loaded"], "tokens": meta["tokens"]}
    if diff:
        turn["diff"] = {**diff, "id": uuid.uuid4().hex[:8], "status": "pending"}
    thread["turns"].append(turn)
    thread["tokens_last"] = meta["tokens"]
    if thread.get("title") in ("", "New thread"):
        thread["title"] = message[:48]
    save_thread(thread)
    return {"reply": reply, "diff": turn.get("diff"), "context": meta}


# ------------------------------------------------------------------ diffs
def apply_diff(thread: dict, diff_id: str, approve: bool, note: str = "") -> dict:
    """Approve writes the change; reject logs why. The constitution is never
    edited without passing through here."""
    for turn in thread.get("turns", []):
        d = turn.get("diff")
        if not d or d.get("id") != diff_id:
            continue
        if d.get("status") != "pending":
            return {"ok": False, "reason": f"already {d['status']}"}

        if not approve:
            d["status"] = "rejected"
            d["note"] = note[:300]
            save_thread(thread)
            try:
                with FEEDBACK.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": time.time(), "kind": "diff_rejected",
                                         "file": d.get("file"), "why": d.get("why"),
                                         "note": note[:300]}) + "\n")
            except OSError:
                pass
            return {"ok": True, "status": "rejected"}

        target = ROOT / str(d.get("file") or "FRAMEWORKS.md").strip()
        # Only the two constitution files are writable from chat.
        if target.name not in {"FRAMEWORKS.md", "EXEMPLARS.md"}:
            return {"ok": False, "reason": f"refusing to write {target.name}"}
        find, replace = str(d.get("find") or ""), str(d.get("replace") or "")
        if not target.exists():
            if target.name == "EXEMPLARS.md":
                target.write_text("# EXEMPLARS\n\n", encoding="utf-8")
            else:
                return {"ok": False, "reason": f"{target.name} not found"}
        text = target.read_text(encoding="utf-8", errors="replace")
        if find and find not in text:
            return {"ok": False, "reason": "the text to replace is no longer in the file"}

        backup = target.with_suffix(target.suffix + f".bak-{int(time.time())}")
        backup.write_text(text, encoding="utf-8")
        target.write_text(text.replace(find, replace, 1) if find else text + "\n" + replace,
                          encoding="utf-8")
        d["status"] = "approved"
        d["applied_at"] = time.time()
        save_thread(thread)
        return {"ok": True, "status": "approved", "file": target.name,
                "backup": backup.name}
    return {"ok": False, "reason": "no such diff"}
