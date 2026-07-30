# Content Machine

Local video → short clips pipeline. Runs entirely on one Windows PC: phone uploads
over home WiFi, transcription and rendering happen on the GPU, and the only thing
that ever leaves the machine is text sent to a language model for ranking and
caption copy. **Source video never touches the cloud.**

Built for one person's workflow. Not a product.

---

## What it does

1. **Upload** from a phone browser on the LAN — chunked and resumable, so a 2 GB
   file survives a dropped connection.
2. **Transcribe** with faster-whisper on CUDA (word-level timestamps).
3. **Find candidates** — complete-thought windows only, never a mid-sentence cut.
4. **Audio gate** — ffmpeg-only loudness/silence check; bad audio gets flagged, not
   silently dropped.
5. **Rank** each candidate against `FRAMEWORKS.md` via a language model, batched to
   ≤3 calls per profile per video.
6. **Caption + review** — hook, caption, niche hashtags, PASS/FLAG verdict. One
   call for the whole video.
7. **Render** straight from source in a single pass — 16:9 plus a face-tracked
   9:16 with burned-in captions.
8. **Approve on the phone** — preview, approve/reject with a required reason,
   download the original bytes with zero transcoding.

## Design rules

- **One encode, from source.** No intermediate re-encodes. crf 16, preset slow.
- **Complete thoughts only.** A clip that starts or ends mid-sentence is not a clip.
- **Call count is the cost.** Each language-model call carries ~22.7k tokens of
  fixed context regardless of prompt size, so batching beats prompt trimming.
- **Rejections must teach.** A reject requires a reason; reasons accumulate in
  `feedback.jsonl` for later framework tuning.
- **Nothing silently succeeds.** A failed stage reports what failed and why rather
  than producing an empty result that looks finished.

## Layout

```
server/          FastAPI app: upload, queue, status, approval UI
  app.py         routes + PIN gate
  jobs.py        single-slot worker, JSON job store
  uploads.py     chunked resumable upload
  net.py         LAN address discovery, URL.txt
pipeline/
  transcribe.py  faster-whisper, GPU with CPU fallback, cached
  segment.py     transcript -> complete-thought candidate windows
  audio_gate.py  ffmpeg loudness/silence QC
  brain.py       language-model calls + usage/cost accounting
  prompts.py     ranking / caption / vision prompt construction
  render.py      single-pass ffmpeg render, face tracking, captions
  postiz_client.py  optional scheduling push (off by default)
postiz/          local Postiz stack (docker compose)
FRAMEWORKS.md    the ranking constitution - edit this to change its taste
```

## Setup

Requires Windows, Python 3.12, ffmpeg, and an NVIDIA GPU (CPU works, slower).

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      :: then set CM_PIN and CM_SESSION_SECRET
```

One-time, as Administrator — firewall scoped to your subnet only, no-sleep, and a
logon task:

```
powershell -ExecutionPolicy Bypass -File setup-windows.ps1
```

Then:

```bat
run.bat
```

It prints the LAN URL to bookmark and writes it to `URL.txt`.

## Configuration

All of it in `.env` (see `.env.example`). The ones that matter:

| Key | Meaning |
|---|---|
| `CM_PIN` | phone login PIN |
| `CM_WHISPER_MODEL` | `medium` by default |
| `CM_CLIP_MIN_S` / `CM_CLIP_MAX_S` | candidate length bounds |
| `CM_MAKE_VERTICAL` | render the 9:16 variant |
| `CM_BRAIN` | `auto` \| `cli` \| `api` |
| `CM_SCHEDULE_DEFAULT` | Postiz push, off by default |

## Status

Working: upload, queue, transcription, candidates, audio gate, ranking, captions,
16:9 render, approval UI, rejection logging.

Known limitations, honestly:

- **Face framing is unreliable.** The Haar cascade returns false positives on wide
  4K room shots. Needs a DNN detector (YuNet) before the 9:16 variant can be
  trusted.
- **Silence removal** is implemented but not yet wired into the render.
- **Punch-in zooms** are implemented but not enabled by default.
- **Candidate lengths skew long**; the distribution needs another pass.
- `tune`, visual QC, exemplar bank, and the performance loop are designed but not
  built.

## Not included

Auto-posting is documented (`STAGE4-NOTES.md`, `POSTIZ-SETUP.md`) but the
scheduling push stays behind a flag. Secrets, private video, transcripts and
rendered clips are gitignored.
