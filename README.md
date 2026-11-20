# Rolecall

Permission-first GTA RP clipping. Rolecall turns authorized streamer footage into captioned 9:16 clips and publishes them to YouTube Shorts and Instagram Reels, with TikTok exports. It also runs a paid clipping service for streamers.

```bash
cp .env.example .env
```

```bash
docker compose up -d --build
```

Then open `http://<host>:8080` and sign in as `admin` with the `ADMIN_PASSWORD` from `.env`. Account setup and launch steps: **[docs/LAUNCH.md](docs/LAUNCH.md)**. Automation (n8n autopilot, alerts, daily report): **[docs/N8N.md](docs/N8N.md)**.

## Pipeline

```
discovery (GTA 6 streams on YouTube/Twitch) → invite → streamer opts in at /p/join → ownership auto-verified
footage (stream watcher: new finished VODs on authorized channels / upload / drop folder / URL / submission)
  → permission check (channel covered? verified? edit+repost+monetize?)
  → transcribe (faster-whisper, local, word timestamps)
  → rank moments: hook (first 3s), context (stands alone, no scene break), payoff (laugh/reaction lands)
  → render 1080x1920: dead-air trim (only where it's actually quiet), split/blur/tracked crop,
    word-synced captions, streamer credit, EBU R128 loudness + denoise
  → checks: rights, PII in speech and on screen (OCR), slurs, music, caption confidence, early violence, demo
  → titles/captions per platform (free rule-based; optional Claude with ANTHROPIC_API_KEY)
  → review queue (first 3 clips per streamer, anything flagged) or auto-approve
  → scheduler: pause switch, daily caps, spacing, posting window, retries with backoff,
    resumable uploads + duplicate guards
  → YouTube (Data API) · Instagram (Graph API, Instagram Login) · TikTok (export or inbox draft)
  → metrics (YouTube Analytics, IG insights, manual TikTok) → "double down" recommendations
```

## Layout

| Path | What |
|---|---|
| `rolecall/pipeline.py` | ingest, process, render, queue, removals, auto-verify |
| `rolecall/rank.py` | moment detection + scoring |
| `rolecall/render.py`, `captions.py` | FFmpeg reframing and PNG caption track (no libass needed) |
| `rolecall/review.py` | pre-publish checks |
| `rolecall/publishers/` | YouTube, Instagram, TikTok |
| `rolecall/scheduler.py` | background worker (covers watch/process/publish when n8n isn't driving) |
| `rolecall/watcher.py` | stream watcher: new, finished VODs from verified channels |
| `rolecall/discover.py` | GTA 6 discovery: finds and ranks streamers to invite (opt-in before any clipping) |
| `rolecall/autopilot.py` | stage runner (watch / process / publish) shared by n8n and the worker |
| `rolecall/web/api.py`, `rolecall/events.py` | bearer-token API for n8n + signed event webhooks to it |
| `n8n/workflows/` | Autopilot, Events → alerts, Daily report, Find GTA 6 streamers (import with `scripts/n8n.sh`) |
| `rolecall/web/` | dashboard + public pages (`/p/join`, `/p/pricing`, `/p/remove`, `/p/privacy`, `/p/terms`, `/d/<token>`) |
| `data/` | SQLite DB, footage, renders, exports (Docker volume) |

## Dev

```bash
uv venv --python 3.12 .venv
```

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
```

```bash
.venv/bin/python -m pytest -m "not slow"
```

```bash
.venv/bin/python -m pytest -m slow
```

```bash
scripts/dev.sh
```

```bash
scripts/n8n.sh
```

`rolecall demo --process` builds a clearly labeled synthetic demo and runs it through the pipeline. Demo clips can never be published.
