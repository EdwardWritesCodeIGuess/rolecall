# AGENTS.md — shared notes for every AI agent working in this folder

Two agents work here: **Claude** (Claude Code) and **GPT** (Codex or similar). Read this file before
changing anything, and update the "Claims" table when you start or finish something.

## What this is
Rolecall: a permission-first GTA RP clipping pipeline + dashboard (Python 3.12, FastAPI, SQLite,
FFmpeg, faster-whisper). Brand handle `rolecallclips`. Full launch guide: `docs/LAUNCH.md`.
Architecture overview: `README.md`.

## Ground rules (avoid stepping on each other)
1. **Claim before editing.** Add a row to Claims with the files/area you're touching. Don't edit files
   another agent has claimed as in progress; leave a note in "Messages" instead.
2. **Never commit or print secrets.** `.env`, `data/` (DB, encrypted tokens, `data/credentials/`) are
   private. Don't paste their contents into chat, logs, or tests.
3. **Don't touch live accounts without the user's say-so.** The brand YouTube/Instagram accounts and the
   Google Cloud project are real. Demo footage must never be published (the code enforces this).
4. **Keep tests green:** `.venv/bin/python -m pytest -m "not slow"` (fast, ~1s) before you hand back.
   `-m slow` runs the full ffmpeg + whisper pipeline (~1 min).
5. The local dev server runs on port 8080 via `scripts/dev.sh` (Claude's browser preview uses it).
   Restart it after changing `.env`; don't start a second server on 8080.
6. Git: private repo **github.com/EdwardWritesCodeIGuess/rolecall** (`main` is protected by habit, not
   settings). Work on a branch named `<agent>/<topic>` (e.g. `gpt/outreach`, `claude/n8n`), open a PR,
   keep CI (`.github/workflows/tests.yml`) green. Small, targeted edits over whole-file rewrites so the
   other agent's changes survive. Pull before you start; rebase on `main` before you open the PR.

## Current state (2026-11-19)
- Pipeline, dashboard, publishers, tests: done (38 fast tests + 1 slow e2e passing).
- YouTube: channel live at youtube.com/@rolecallclips. Google Cloud project `rolecall-515223` with
  YouTube Data + Analytics APIs, OAuth web client (redirect `http://localhost:8080/oauth/youtube/callback`),
  app in **Testing** mode with elink9004@gmail.com as test user. Credentials are in `.env`.
  **Connected** on the local server (token in `data/rolecall.db`, encrypted with `.env` SECRET_KEY);
  live API call confirmed channel "Rolecall" @rolecallclips. Testing-mode refresh tokens expire after
  7 days → publish the OAuth app once Branding has public home/privacy/terms URLs.
- Instagram: @rolecallclips is a public Creator account (Teen Account — keep it that way). **User chose
  manual posting**: `instagram_mode` defaults to `export`; no Meta developer app. Don't set up the
  Instagram API unless the user asks.
- TikTok: skipped (sign-up requires the mobile app; user doesn't want it). Disabled in settings.
- Homelab: tailnet machines `general` (100.108.107.68), `mediaserver`, `nas-1` answer SSH but reject
  this Mac's key as `edwardlink`. Old k3s hosts in ~/.ssh/config are gone. Need the right user/key.
- No authorized streamer footage yet → nothing published.

## Open work
| Task | Notes |
|---|---|
| Connect YouTube OAuth on the local server | Done 2026-11-19 (Claude) |
| ~~Meta developer app + Instagram token~~ | Dropped: user posts Instagram by hand |
| Public HTTPS (Tailscale Funnel on homelab) | Then fill Google Branding + publish OAuth app to production |
| Deploy to homelab with docker compose | Blocked on SSH user/key |
| Streamer outreach list + DM templates | Research only; sending messages needs the user's approval |
| Profile images on YouTube/Instagram | User uploads (browser pane can't open file pickers) |

## Claims
| Agent | Area / files | Status | Updated |
|---|---|---|---|
| Claude | YouTube OAuth connect; `rolecall/web/app.py` (OAuth callbacks now public + single-use state), `tests/test_web.py` | done | 2026-11-19 |
| Claude | Instagram manual-posting default (`rolecall/db.py`, `docs/LAUNCH.md`) | done | 2026-11-19 |
| Claude | GitHub repo + CI | done | 2026-11-19 |
| Claude | n8n autopilot: `rolecall/web/api.py`, `rolecall/autopilot.py`, `rolecall/events.py`, `rolecall/watcher.py`, `n8n/`, `docs/N8N.md`, docker-compose n8n service; on-screen PII OCR in `rolecall/review.py`; credit guard in `rolecall/copywriter.py` | in progress | 2026-11-19 |

## Messages
- Claude → GPT: hi! Pick any unclaimed task above and add a Claims row. The user wants to be hands-off,
  so batch anything that needs them into one clear list.
- Claude → GPT (2026-11-19): the user says you're working in the repo too. My n8n work is on branch
  `claude/n8n-autopilot` (PR coming). Until it merges, please don't edit `rolecall/scheduler.py`,
  `rolecall/pipeline.py`, `rolecall/review.py`, `rolecall/copywriter.py`, `rolecall/db.py` (SCHEMA +
  DEFAULT_SETTINGS), `rolecall/web/app.py` (bottom: router includes) or anything under `n8n/`; ping here
  if you need to. Use your own clone or `git worktree`, not the user's working copy at
  /Users/edwardlink/Clipping (I work there). Good unclaimed areas: streamer outreach list + DM templates
  (`docs/`), pricing/landing copy (`rolecall/web/templates/public_*.html`), homelab deploy.
