# n8n autopilot

n8n drives the clipping loop; Rolecall does the heavy work (download, transcribe, cut, render, check, post).
The machine API is the contract between them: [`rolecall/web/api.py`](../rolecall/web/api.py).

```
n8n "Autopilot" (every 15 min)
  POST /api/v1/run/watch     find new, finished VODs on authorized streamers' channels (+ inbox folder)
  POST /api/v1/run/process   cut & edit the next VOD: moments → 9:16 reframe around the action,
                             captions, streamer credit, audio cleanup, checks, per-platform titles
  POST /api/v1/run/publish   post what's approved and due (YouTube automatic; Instagram/TikTok
                             prepared for posting by hand, per Rolecall's settings)

Rolecall → n8n "Events → alerts" (webhook /webhook/rolecall-events)
  clip.needs_review   flags (rights, music, personal info in speech or on screen, captions) + review link
  post.export_ready   caption to paste + video link, for manual Instagram/TikTok posting
  post.published / post.failed / job.failed / source.ingested / clip.ready

n8n "Daily report" (9:00)   GET /api/v1/report + /api/v1/targets → summary to your alert channel

n8n "Find GTA 6 streamers" (every 3 h)
  POST /api/v1/run/discover  search YouTube (and Twitch, once keys are set) for live + recent GTA 6
                             streams, rank the streamers, alert about the best new ones with an invite
```

## Finding streamers (GTA 6)

Discovery finds streamers; it never clips them. The funnel:

1. **Found**: YouTube Data API search (`discover_youtube_query`, live + last 48 h, ~200 quota units per run,
   the daily quota is 10,000 and one upload costs 1,600) and Twitch's "Grand Theft Auto VI" category
   (`TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET`, free app at dev.twitch.tv). Ranked 0-1 by fit: mid-size
   (~800 live viewers is the sweet spot), seen on several days, your language (`discover_languages`),
   gameplay rather than trailer reactions or news.
2. **Invited**: the dashboard's *Find streamers* page and the n8n alert give a personal invite per streamer
   (`invite_template`) with a `/p/join?ref=...` link that prefills the sign-up form. Someone sends it on the
   streamer's socials and marks it *invited*. Rolecall doesn't send messages itself.
3. **Joined**: the streamer signs up; the prospect links to the new creator.
4. **Automatic**: once the ownership code shows on their channel (auto-checked every 30 min) or you press
   Verify, the stream watcher turns on for their channel, and their streams flow through
   find → cut & edit → review → post with no further steps.

Invite links need `PUBLIC_BASE_URL` (Tailscale Funnel / Cloudflare Tunnel); without it they point at
localhost and the page says so.

## Where it posts

n8n doesn't keep its own account list. It reads Rolecall's: `GET /api/v1/targets` returns the brand
handle, which platforms are enabled, automatic vs. by-hand mode, connection status, daily limits and the
posting window. Change those in the Rolecall dashboard (Overview → settings, Connections), not in n8n.

## Who drives

Setting `orchestrator` (`rolecall set orchestrator '"auto"'`):

| value | behavior |
|---|---|
| `auto` (default) | n8n drives while it keeps calling `/api/v1/run/*`. If it is quiet for `n8n_grace_minutes` (90), Rolecall's built-in worker takes over, so a dead n8n never stalls the channel. |
| `n8n` | only n8n runs watch/process/publish |
| `builtin` | only the built-in worker does (n8n is just alerts/reports) |

`watch` and `process` share one lock (never two transcodes at once); `publish` has its own, so posting
continues while a long VOD renders. A busy stage returns HTTP 409 `{"status": "busy"}`; the workflow
nodes are set to never error, so the next run simply tries again.

## Run it locally (this Mac)

```bash
scripts/dev.sh
```

```bash
scripts/n8n.sh
```

The second command installs n8n into `n8n/.runtime` the first time, imports + publishes the three
workflows from `n8n/workflows/`, and serves the editor on http://localhost:5678 (it asks you to create
an owner login on first visit). Both read `.env`: `ROLECALL_API_TOKEN` (shared secret),
`N8N_WEBHOOK_URL` (where Rolecall sends events) and optional `NOTIFY_WEBHOOK` (Discord/Slack/ntfy) for alerts.

After editing a workflow in the n8n editor, export it back so the repo stays the source of truth:

```bash
N8N_USER_FOLDER=$PWD/n8n/data n8n/.runtime/node_modules/.bin/n8n export:workflow --id=RolecallAutopilt --output=n8n/workflows/01-rolecall-autopilot.json --pretty
```

Re-import the repo's versions with `scripts/n8n.sh --reimport`.

## Run it on the homelab (Docker)

```bash
docker compose up -d --build
```

```bash
docker compose --profile setup run --rm n8n-import
```

```bash
docker compose restart n8n
```

`docker-compose.yml` wires `ROLECALL_URL=http://rolecall:8080` and
`N8N_WEBHOOK_URL=http://n8n:5678/webhook/rolecall-events`; both services read the same `.env`.

## API quick reference

All calls need `Authorization: Bearer $ROLECALL_API_TOKEN`.

| Call | Use |
|---|---|
| `GET /api/v1/status` | paused?, driver, platform readiness, queue counts, running stages |
| `GET /api/v1/targets` | where to post (see above) |
| `GET /api/v1/creators` | authorized streamers, channels, credit lines, permission scope |
| `POST /api/v1/run/{watch,process,publish,discover}?wait=N` | run a stage; `wait` blocks up to N seconds for the result |
| `GET /api/v1/jobs/{id}` | stage result if you didn't wait |
| `GET /api/v1/clips?status=needs_review` | clips with flags, copy, links |
| `POST /api/v1/clips/{id}/approve` / `reject` | review from automation (approve accepts `{"overrides": {"youtube.title": "..."}}`) |
| `GET /api/v1/posts?status=export_ready` | posts waiting to be posted by hand |
| `POST /api/v1/posts/{id}/posted` `{"url": "https://..."}` | record a manual post |
| `POST /api/v1/pause` `{"paused": true, "platform": "youtube"}` | kill switch (platform optional) |
| `GET /api/v1/report?days=1` | numbers for reports |
| `GET /api/v1/prospects?status=new` | streamers found by discovery, with invite text + link |
| `POST /api/v1/prospects/{id}/{invited,ignored,declined,new}` | move a prospect through the funnel |

Events are POSTed as JSON with `X-Rolecall-Signature: sha256=<HMAC of the body with the API token>`.

## Guardrails that stay in Rolecall (n8n can't bypass them)

- Only creators with an active, **verified** permission are watched, ingested or posted; every VOD URL
  must sit on a channel the permission covers. Streams still live are skipped until the VOD is finished.
- Demo footage never publishes. Posts re-check permission right before upload.
- First `trusted_after` (3) clips per streamer, and any clip with a `review` or `block` flag, wait for a
  human: music, personal info in speech or on screen (OCR via tesseract), slurs, low caption confidence,
  uncertain rights. Approving can't override missing rights.
- Every platform's caption carries the streamer credit; the credit is also burned into the video.
