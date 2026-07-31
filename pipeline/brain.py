"""Brain calls.

Routes through the Claude Code CLI in headless JSON mode by default, so calls are
covered by the Pro plan rather than billed per token. ANTHROPIC_API_KEY is the
fallback.

Measured cost note: each CLI invocation carries ~22.7k tokens of fixed harness
context regardless of prompt size, so CALL COUNT dominates the bill, not prompt
length. That is why ranking is batched to <=3 calls and captions to 1 call per
video. Always run with cwd=project root — running inside another repo pulls that
repo's CLAUDE.md/MCP config into every call (measured: +2.8k tokens, +$0.028).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
USAGE_LOG = ROOT / "data" / "logs" / "brain-usage.jsonl"


class BrainError(RuntimeError):
    pass


def _cli_path() -> Optional[str]:
    appdata = os.environ.get("APPDATA")
    if appdata:
        cand = Path(appdata) / "npm" / "claude.cmd"
        if cand.exists():
            return str(cand)
    return shutil.which("claude")


def available() -> dict:
    return {"cli": _cli_path(), "api_key": bool(os.environ.get("ANTHROPIC_API_KEY"))}


def _strip_fence(text: str) -> str:
    """Models wrap JSON in ```json fences often enough that this must be handled
    rather than treated as a failure."""
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _first_json(text: str) -> Any:
    """Pull the first complete JSON value out of a response, tolerating prose
    around it. Brace-matching rather than regex so nested objects survive."""
    t = _strip_fence(text)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        if start < 0:
            continue
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(t[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise BrainError(f"no parseable JSON in response: {t[:300]!r}")


def _log_usage(entry: dict) -> None:
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# The CLI ships every tool definition in the system prompt. Text-only calls never
# use them, and stripping them measured a 45% context cut (34.4k -> 18.9k tokens)
# with no change in output quality.
_NO_TOOLS = ("Bash,Read,Write,Edit,MultiEdit,Glob,Grep,WebFetch,WebSearch,"
             "Task,TodoWrite,NotebookEdit,BashOutput,KillShell")


def _call_cli(prompt: str, model: str, timeout_s: int, images: list[str] | None,
              no_tools: bool = False) -> dict:
    cli = _cli_path()
    if not cli:
        raise BrainError("claude CLI not found")
    cmd = [cli, "-p", "--output-format", "json", "--strict-mcp-config"]
    if no_tools and not images:   # vision needs file reading; text calls don't
        cmd += ["--disallowedTools", _NO_TOOLS]
    if model:
        cmd += ["--model", model]
    # Vision: the CLI reads local files when the prompt references their paths and
    # the dir is readable, so images are passed as absolute paths in the prompt.
    if images:
        listing = "\n".join(f"- {p}" for p in images)
        prompt = f"{prompt}\n\nImages to inspect (read these files):\n{listing}\n"
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout_s,
                          cwd=str(ROOT))
    if proc.returncode != 0:
        raise BrainError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise BrainError(f"CLI envelope not JSON: {proc.stdout[:300]!r}") from exc
    if env.get("is_error"):
        raise BrainError(f"claude reported an error: {str(env.get('result'))[:300]}")
    u = env.get("usage", {}) or {}
    return {
        "text": env.get("result", ""),
        "cost_usd": float(env.get("total_cost_usd") or 0.0),
        "in_tokens": int(u.get("input_tokens") or 0),
        "cache_create": int(u.get("cache_creation_input_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "out_tokens": int(u.get("output_tokens") or 0),
        "route": "cli",
    }


_API_MODELS = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5-20251001"}


def _call_api(prompt: str, model: str, timeout_s: int) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise BrainError("no ANTHROPIC_API_KEY")
    import urllib.request  # noqa: PLC0415 - only needed on the fallback path
    body = json.dumps({
        "model": _API_MODELS.get(model, model or "claude-sonnet-5"),
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        env = json.loads(r.read())
    text = "".join(b.get("text", "") for b in env.get("content", []) if b.get("type") == "text")
    u = env.get("usage", {}) or {}
    return {"text": text, "cost_usd": 0.0, "route": "api",
            "in_tokens": int(u.get("input_tokens") or 0), "cache_create": 0,
            "cache_read": 0, "out_tokens": int(u.get("output_tokens") or 0)}


def ask_json(prompt: str, *, label: str, model: str = "", mode: str = "auto",
             timeout_s: int = 900, images: list[str] | None = None,
             retries: int = 2, job_id: str = "", no_tools: bool = True) -> Any:
    """One brain call returning parsed JSON. Retries on a malformed answer with an
    explicit reminder, because a single bad parse should not sink a whole video."""
    last: Exception | None = None
    avail = available()
    use_cli = mode in {"auto", "cli"} and avail["cli"]
    if mode == "api" or (mode == "auto" and not use_cli):
        use_cli = False

    for attempt in range(1, retries + 2):
        p = prompt if attempt == 1 else (
            prompt + "\n\nIMPORTANT: your previous reply was not valid JSON. "
                     "Reply with ONLY the JSON value. No prose, no code fence.")
        t0 = time.time()
        try:
            res = (_call_cli(p, model, timeout_s, images, no_tools) if use_cli
                   else _call_api(p, model, timeout_s))
            parsed = _first_json(res["text"])
            _log_usage({"ts": time.time(), "job": job_id, "label": label, "model": model,
                        "route": res["route"], "attempt": attempt, "ok": True,
                        "wall_s": round(time.time() - t0, 1), "cost_usd": res["cost_usd"],
                        "in": res["in_tokens"], "cache_create": res["cache_create"],
                        "cache_read": res["cache_read"], "out": res["out_tokens"],
                        "images": len(images or [])})
            return parsed
        except Exception as exc:  # noqa: BLE001 - retry any failure shape
            last = exc
            _log_usage({"ts": time.time(), "job": job_id, "label": label, "route":
                        "cli" if use_cli else "api", "attempt": attempt, "ok": False,
                        "wall_s": round(time.time() - t0, 1), "error": str(exc)[:300]})
            # A missing CLI is not retryable; fall to the API if it's configured.
            if use_cli and isinstance(exc, BrainError) and "not found" in str(exc):
                if avail["api_key"]:
                    use_cli = False
                    continue
                break
            time.sleep(min(4 * attempt, 12))
    raise BrainError(f"{label}: brain call failed after {retries + 1} attempts: {last}")


def usage_summary(job_id: str = "") -> dict:
    """Per-video cost rollup for the report."""
    if not USAGE_LOG.exists():
        return {"calls": 0, "cost_usd": 0.0}
    calls, cost, tok_in, tok_out = 0, 0.0, 0, 0
    for line in USAGE_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if job_id and e.get("job") != job_id:
            continue
        if not e.get("ok"):
            continue
        calls += 1
        cost += float(e.get("cost_usd") or 0.0)
        tok_in += int(e.get("in") or 0) + int(e.get("cache_create") or 0) + int(e.get("cache_read") or 0)
        tok_out += int(e.get("out") or 0)
    return {"calls": calls, "cost_usd": round(cost, 4), "in_tokens": tok_in, "out_tokens": tok_out}
