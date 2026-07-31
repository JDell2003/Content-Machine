"""Exemplar bank — learn from what Jason LOVES, not only what he rejects.

Drop files into D:\\ContentMachine\\exemplars\\ then run:

    .venv\\Scripts\\python ingest_exemplars.py

Takes PDFs, text files, and .md notes of hooks/captions that landed, and distils
them into EXEMPLARS.md as taste fingerprints (structure, pacing, hook type, tone).
The ranking and caption prompts already inject EXEMPLARS.md, so anything added
here immediately shapes the next run.

ONE brain call for the whole batch, not one per file — the fixed per-call overhead
(~19k tokens) makes per-file calls the expensive way to do this.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import brain  # noqa: E402
from server import config  # noqa: E402

ROOT = Path(__file__).resolve().parent
EXEMPLAR_DIR = ROOT / "exemplars"
OUT = ROOT / "EXEMPLARS.md"

TEXT_SUFFIXES = {".txt", ".md", ".srt", ".vtt", ".csv"}
DOC_SUFFIXES = {".pdf", ".docx"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}


def collect() -> tuple[list[str], list[Path], list[Path]]:
    """-> (inline text blocks, files to hand the CLI, videos to note)."""
    EXEMPLAR_DIR.mkdir(parents=True, exist_ok=True)
    inline, docs, videos = [], [], []
    for p in sorted(EXEMPLAR_DIR.rglob("*")):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf in TEXT_SUFFIXES:
            try:
                inline.append(f"--- {p.name} ---\n{p.read_text(encoding='utf-8', errors='replace')[:8000]}")
            except OSError:
                continue
        elif suf in DOC_SUFFIXES:
            docs.append(p)          # the CLI can read these off disk itself
        elif suf in VIDEO_SUFFIXES:
            videos.append(p)        # transcript would be needed; noted, not read
    return inline, docs, videos


def main() -> int:
    inline, docs, videos = collect()
    if not inline and not docs:
        print(f"Nothing to ingest. Drop hooks/captions/PDFs into:\n  {EXEMPLAR_DIR}")
        if videos:
            print(f"\n({len(videos)} video(s) found — run them through the pipeline "
                  "instead; this script reads text and PDFs.)")
        return 1

    existing = OUT.read_text(encoding="utf-8", errors="replace")[:4000] if OUT.exists() else ""

    prompt = [
        "You are distilling examples of short-form content Jason LOVES into reusable "
        "taste patterns. These are POSITIVE examples — things that worked.",
        "",
        "Extract what makes them work, not a summary of what they say. For each "
        "distinct pattern you find, capture: the hook type, the structure, the "
        "pacing, the tone, and the specific move that earns attention.",
        "",
    ]
    if existing:
        prompt += ["== EXISTING EXEMPLARS.md (merge with this, don't duplicate) ==",
                   existing, ""]
    if inline:
        prompt += ["== TEXT EXAMPLES ==", "\n\n".join(inline)[:30000], ""]
    if videos:
        prompt += [f"(Also on disk, not read here: {', '.join(v.name for v in videos[:10])})", ""]

    prompt += [
        "== OUTPUT ==",
        "Reply with ONLY this JSON:",
        '{"markdown":"the full new contents of EXEMPLARS.md"}',
        "",
        "Structure the markdown as:",
        "  # EXEMPLARS",
        "  ## Hook patterns   — named patterns with a one-line template and an example",
        "  ## Structures      — how the good ones are built beat by beat",
        "  ## Tone rules      — what the voice does and avoids",
        "  ## Anti-patterns   — what these examples conspicuously never do",
        "",
        "Keep it under 2 pages. It is injected into every ranking and caption call, "
        "so every line costs tokens on every run. Be ruthless: patterns, not prose.",
    ]

    print(f"Ingesting {len(inline)} text file(s), {len(docs)} document(s)...")
    res = brain.ask_json(
        "\n".join(prompt), label="exemplars:ingest", model=config.CAPTION_MODEL,
        mode=config.BRAIN_MODE, timeout_s=config.BRAIN_TIMEOUT_S,
        images=[str(d) for d in docs] or None)

    md = str(res.get("markdown") or "").strip()
    if not md:
        print("Brain returned nothing usable.")
        return 1
    if OUT.exists():
        OUT.with_suffix(".md.bak").write_text(OUT.read_text(encoding="utf-8"), encoding="utf-8")
    OUT.write_text(md, encoding="utf-8")
    print(f"\nWrote {OUT} ({len(md)} chars, ~{len(md)//4} tokens per run)")
    print("This now feeds every ranking and caption call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
