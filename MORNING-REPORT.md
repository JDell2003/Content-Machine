# MORNING REPORT — overnight autonomous run

**Headline: the engine runs end-to-end.** Video in → transcript → candidates →
audio gate → ranked → captioned → rendered clip on disk, verified on a real
speech recording. Calibration did **not** run: your Etavis file never arrived.

---

## THE NEXT THREE TAPS (in this order)

**1. Run the admin script.** Your phone still cannot reach the server. The live
firewall rule is still the old VPN-scoped one from the Tailscale era, and
`192.168.7.x` is not in that range.
```
powershell -ExecutionPolicy Bypass -File D:\ContentMachine\setup-windows.ps1
```

**2. Upload the Etavis file** at **http://192.168.7.153:3000** (PIN is in `.env` / `URL.txt`, not in git).
Pick **BOTH** on the profile selector. Calibration then runs by itself and
produces two ranked lists.

**3. Read `FRAMEWORKS.md` and mark it up.** One real test run shows the ranker
follows it closely — including a hard-filter judgment I did not hand-code (see
"What the machine did well"). Your edits to that file are now the highest-leverage
thing you can do.

---

## STATUS PER COMPONENT

### Done and verified
| Component | Evidence |
|---|---|
| LAN-only server, PIN, port 3000 | 16/17 acceptance checks over `192.168.7.153` |
| Chunked resumable upload | killed mid-upload → resumed from gap; refuses incomplete (409) |
| Byte-identical assembly | verified against source size |
| Job queue, one at a time | orphan jobs re-queue after restart |
| Stable URL | console banner + `URL.txt` + Jobs page; `jasono.local:3000` also answers |
| Tailscale gone | you uninstalled it; verified no service/process/adapter/`100.x` |
| Whisper on the 3060 | `cuda/float16`, 45 segments from real speech, ~5.7x realtime, cached to disk |
| Candidate segmentation | complete-thought windows, aha-cue detection, overlap suppression |
| Audio sanity gate | caught real clipped peaks (−0.2 dBFS) on the test file |
| Ranking brain, both profiles | 2 rank calls + 1 caption call, JSON parsed, scores applied |
| Captions + reviewer | hook, caption, 7 niche hashtags, PASS/FLAG with reason |
| Renderer, single pass | 107 MB 1080p clip from source, one encode, crf16/slow |
| Profile selector | required on upload; BRAND/TRAINER/BOTH + RAW checkbox |
| Rejection memory | reason required; appends to `feedback.jsonl` |
| STAGE4-NOTES.md | costed + full Meta click-path |

### Stubbed / not built
| Component | Why |
|---|---|
| `tune` command | needs accumulated rejections to be worth anything — you have zero so far |
| UP2 visual QC | keyframe extractor + vision prompt written; **not wired** (gated on calibration) |
| UP2 punch-ins | ffmpeg filter written and callable; **not enabled** by default |
| UP2 karaoke captions | SRT writer works, burns into 9:16; untested on real faces |
| UP2 exemplar bank | prompt injection ready, `EXEMPLARS.md` absent, no page yet |
| UP3 (all) | gated behind UP2 |
| Postiz | **not deployed** — see money decisions |
| Schedule toggle | not built; design in STAGE4-NOTES |

### Blocked
- **Calibration** — Etavis file absent. `data/uploads/` holds only my 7 MB test clips.
- **Postiz $0 path** — Docker is **not installed** on this PC, and installing
  Docker Desktop needs admin + WSL2 + a reboot. I did not do that to a sleeping
  machine. `cloudflared` also not installed.

---

## WHAT THE MACHINE DID WELL (unprompted)

On the test recording it ranked the planted teach→aha exchange #1, tagged it
`teach-aha`, and wrote:

> **hook:** "Curling a dumbbell doesn't prove you're worth paying"

Then it **FLAGGED its own top clip** with this:

> strong teach→aha pair in first ~40s (BRAND's top signal), but at 'now let me
> talk about food' it pivots into pure diet advice with no business angle — that
> back half is TRAINER material bolted onto a BRAND clip; cut around the pivot
> and post the first half alone.

Nothing in my code detects profile bleed. It inferred that from your TRAINER hard
filter in FRAMEWORKS.md and applied it in reverse. That is the calibration signal
working before calibration.

---

## BUGS FOUND AND FIXED

1. **OpenCV 5.x moved `CascadeClassifier`** — face tracking crashed. Pinned to
   OpenCV 4.14 and added a probe that degrades to "skip 9:16" instead of raising.
2. **Too-narrow `except` destroyed good work** — a face-detection crash threw away
   a 16:9 clip that had *already rendered successfully*. Both render blocks now
   catch broadly: the 16:9 cut is the deliverable and a 9:16 problem must never
   discard it.
3. **Middleware order** (earlier) — auth ran before the session existed; every
   page 500'd.
4. **Loopback bind** (earlier) — why your phone couldn't connect the first time.

---

## COST — recomputed after the batching change

Measured, not estimated: **3 calls = $0.3183 notional** (85.5k in / 3.1k out).

Per-call fixed overhead is ~22.7k tokens of harness context regardless of prompt
size, so **call count is the whole game**.

| Run | Calls | API-billed | Pro-headless |
|---|---|---|---|
| One profile | 3 rank + 1 caption = **4** | **$0.40–0.70** | $0 marginal |
| BOTH profiles | 6 rank + 2 caption = **8** | **$0.80–1.45** | $0 marginal |
| + UP2 vision | +2 | +$0.15–0.30 | $0 marginal |

**Priority 5 saving:** captions were one call per clip. Batched to one call per
video, 8 clips → **7 fewer calls, ~$0.70–1.20 saved per profile per video**. Same
output quality; the reviewer arguably got better because it sees all clips at once
and can say "this one duplicates that one".

---

## MONEY DECISIONS PENDING

**1. Postiz hosting — I did not spend your money.**
- Railway lightweight (Postiz + Postgres + Redis): **$5–12/mo**
- Railway with Temporal (8 services): $20–35/mo — avoid
- **The catch:** Railway's $5 credit is per *workspace*, and RiseForIt already
  burns it. Check Railway → Usage before deciding.

**2. The $0 path is real but needs two installs I wouldn't do unattended:**
Docker Desktop (admin + WSL2 + reboot) and a tunnel for Meta's HTTPS OAuth
callback. My recommendation: **Cloudflare Tunnel with a named tunnel**, not
ngrok — a *named* tunnel gives a stable hostname that survives restarts, and
Meta's redirect URI must match character-for-character. ngrok's free random
subdomain changes every restart and would break OAuth on every reboot.

**Recommendation:** do the $0 Docker + named Cloudflare Tunnel path. It keeps
clips off the cloud entirely and costs nothing. It needs ~20 min of your admin
taps. Say go and I'll script it.

---

## QUESTIONS I NEED ANSWERED

1. **Postiz: $0 local Docker path, or $5–12/mo Railway?** (recommendation above)
2. **Punch-ins default on or off?** Written but not enabled. They re-encode, which
   is fine, but they're an editorial choice and I'd rather you see one before it
   becomes default.
3. **Karaoke captions default ON for 9:16 — confirm?** Your spec said yes; I've
   built it that way but it's untested on real faces.
4. **Clip cap of 8 per profile per video** — right number? A 70-min meeting might
   deserve more; more clips cost nothing extra in brain calls now that captions
   are batched.
5. **`MAKE_VERTICAL` when no faces are found** — currently skips 9:16 entirely.
   Alternative is a centre-crop. Skip, or centre-crop?

---

## KNOWN LIMITATION WORTH FLAGGING

The test recording produced only **2 candidates from 125 seconds** — correct
behaviour for 30–80s windows in a 2-minute file, but it means **the candidate
generator is not yet proven at scale**. A 70-minute meeting should yield 40–60
candidates, which is exactly what the ≤3 rank-call batching was built for. That
path is untested until your file lands. If calibration misses your timestamp
targets, the first thing I'll check is whether the segmenter is producing windows
at those timestamps at all — a ranking miss and a segmentation miss look identical
from the ranked list.

---

## ONE COMMAND

`D:\ContentMachine\run.bat` — running now, minimized. Survives reboot via the
logon task once you run the admin script.
