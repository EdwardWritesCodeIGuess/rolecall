# Rolecall launch guide

Rolecall is a GTA RP clipping channel, **@rolecallclips** on all three platforms, plus a paid clipping service for streamers.
This guide lists every step that needs you: account sign-ups, verification and app approvals. Everything else runs on its own.

Brand kit (in `assets/brand/`):

| Item | Value |
|---|---|
| Name | Rolecall |
| Handle | `rolecallclips` (free on TikTok, Instagram and YouTube as of 19 Nov 2026; backup `incharacterclips`) |
| Profile image | `assets/brand/profile_1080.png` (designed to survive the circle crop) |
| YouTube banner | `assets/brand/banner_2560x1440.png` |
| Short bio (TikTok / IG, 80 chars) | `GTA RP's best moments, clipped with permission 🎬 Streamers: link below` |
| Long bio (YouTube) | `The funniest, wildest and most unexpected GTA roleplay moments, clipped with each streamer's permission and always credited. Streamers: get clipped or hire us at <PUBLIC_BASE_URL>/p/join` |
| Link in bio | `<PUBLIC_BASE_URL>/p/pricing` |

---

## 1. Run it on the homelab (10 min)

```bash
scp -r ~/Clipping homelab:~/rolecall
```

```bash
ssh homelab
```

```bash
cd ~/rolecall && cp .env.example .env && nano .env
```

In `.env`, set `ADMIN_PASSWORD` and `SECRET_KEY`. Generate the key with:

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(40))"
```

Then start it:

```bash
docker compose up -d --build
```

Open `http://<homelab-ip>:8080` and sign in as `admin` with your `ADMIN_PASSWORD`.
Back up `data/` (the database, footage, encrypted tokens). Keep `SECRET_KEY` safe: if you lose it, you'll have to reconnect the accounts.

## 2. Public HTTPS address (needed for Instagram + streamer pages)

Instagram fetches each Reel from a URL, and streamers need to reach `/p/join`. Choose one free option:

- **Tailscale Funnel** (simplest if you already use Tailscale): run `tailscale funnel --bg 8080` on the homelab. Then set `PUBLIC_BASE_URL=https://<machine>.<tailnet>.ts.net`.
- **Cloudflare Tunnel** (needs a domain on Cloudflare): create a tunnel in the Zero Trust dashboard pointing at `http://rolecall:8080` and put its token in `CLOUDFLARE_TUNNEL_TOKEN`. Then run `docker compose --profile tunnel up -d` and set `PUBLIC_BASE_URL=https://clips.<yourdomain>`.

Then restart with `docker compose up -d`.

## 3. Brand accounts (yours: I can't do sign-ups, CAPTCHAs, or verification codes)

Use **elink9004@gmail.com** for all of them, with handle **rolecallclips**. Use the profile image and bios above.

1. **YouTube.** Sign in to Google as elink9004@gmail.com, then go to youtube.com → profile icon → *Create a channel*. Name it `Rolecall`, use handle `@rolecallclips`, add the image, banner and description. Verifying a phone number at youtube.com/verify unlocks the full feature set.
2. **Instagram.** Sign up with the email, using username `rolecallclips`. Then go to Settings → *Account type and tools* → *Switch to professional account* → **Creator** → category *Gaming video creator*. The API only works with professional accounts.
3. **TikTok.** Sign up with the email, using username `rolecallclips`. Keep it a **personal** account: Creator Rewards excludes business accounts.

Turn on 2-step verification on each account. Keep the recovery email and phone as yours.

## 4. Connect YouTube (auto-posting works)

1. Go to <https://console.cloud.google.com> (signed in as elink9004@gmail.com) → create project **rolecall**.
2. Under *APIs & Services → Library*, enable **YouTube Data API v3** and **YouTube Analytics API**.
3. Set up the *OAuth consent screen*:
   - External, app name **Rolecall**, your email as support/developer contact
   - Privacy policy URL `<PUBLIC_BASE_URL>/p/privacy`
4. Under *Audience*, click **Publish app** (set it to *In production*). In "Testing" mode, Google expires the login after 7 days.
5. Go to *Credentials → Create credentials → OAuth client ID → Web application*. Add an authorized redirect URI that **exactly** matches what Dashboard → Connections shows. It's `BASE_URL/oauth/youtube/callback`. Google only accepts `http://localhost...` or `https://...`, so either:
   - set `BASE_URL=http://localhost:8080`, and use the "paste the URL" box if the redirect page fails to load, or
   - set `BASE_URL` to your `PUBLIC_BASE_URL`.
6. Put the client ID and secret in `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, then run `docker compose up -d`.
7. In the Dashboard, go to Connections → **Connect YouTube** and sign in as the brand account. You'll see "Google hasn't verified this app". Click *Advanced → Go to Rolecall*: it's your own app.

Quota: by default, about 100 uploads per day per project, far more than needed.
Google's current `videos.insert` page says uploads from unverified projects are *not* forced to private. If your first upload still comes back private-locked, file the API audit at <https://support.google.com/youtube/contact/yt_api_form>.

## 5. Instagram (posting by hand, by choice)

**Status 19 Nov 2026:** @rolecallclips is a public Creator account (Gaming video creator) with its bio. The profile photo still needs uploading. It's a *Teen Account*, kept that way on purpose.

**Decision:** post Instagram clips by hand. Automatic posting needs a Meta developer app, which needs a Facebook login, and Meta may restrict API access for teen accounts. Rolecall's `instagram_mode` defaults to **export**:
1. Approved clips wait under Posts → *Ready to post by hand*.
2. Click **Download video** and **Copy** the caption.
3. Upload the video at instagram.com (Create → Post; videos post as Reels).
4. Paste the post link back into **Mark live**, so metrics stay complete.

That's about 30 seconds per clip, capped by the daily limit.

The automatic setup below stays here in case you change your mind.


1. Go to <https://developers.facebook.com>. Register as a developer: Meta requires a Facebook login, so use yours or a new one.
2. **Create app** → use case *Manage messaging & content on Instagram*.
3. Open *Instagram → API setup with Instagram login*:
   - Under *Roles*, add `rolecallclips` as an **Instagram Tester**.
   - Accept the invite in the Instagram app: Settings → *Apps and websites* → *Tester invites*.
4. Click *Generate token* next to the account and approve the permissions:
   - `instagram_business_basic`
   - `instagram_business_content_publish`
   - `instagram_business_manage_insights` (for metrics)
5. In the Dashboard, go to Connections → **Instagram** → paste the token. Tokens last 60 days, and Rolecall refreshes them weekly.

No App Review is needed while the app only posts to accounts you own that have a role on the app (Standard Access). Meta limits API posting to 100 posts per 24 hours.

## 6. TikTok (skipped for now)

**Status 19 Nov 2026:** TikTok is turned off in Rolecall. Its web sign-up stopped at "For security purposes, continue on the TikTok app", and you'd rather not install the app. To add TikTok later, create the account (TikTok may allow the web flow on another try or with a phone number), tick *tiktok* under Overview → Publishing rules, and post exports from desktop at tiktok.com/upload.


TikTok's Content Posting API only lets *audited* apps post publicly. Its guidelines reject "a utility tool to help upload contents to the account(s) you or your team manages", so automatic public posting isn't available for a single-owner tool. What Rolecall does instead:

- **Export mode (default, nothing to set up).** Each approved clip shows under Posts → *Ready for TikTok*, with the video and a caption you can copy. Post it from your phone, then paste the link back so metrics are tracked. About 30 seconds per clip.
- **Inbox mode (optional).** Create an app at developers.tiktok.com:
  - Products: Login Kit + Content Posting API (*upload*, not direct post)
  - Redirect URI `<PUBLIC_BASE_URL>/oauth/tiktok/callback`
  - Privacy and terms URLs from `/p/privacy` and `/p/terms`
  - Request the `video.upload` scope and submit for review
  
  Once approved, add `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` to `.env` and click *Connect TikTok app*. Clips then arrive in your TikTok inbox as drafts, and you tap to post.
- **Paid alternative.** Some schedulers have audited TikTok apps (Buffer, Metricool, Later). I didn't set any up because you asked me to check before adding paid services.

## 7. Monetization: what's real (checked 19 Nov 2026)

- **YouTube Partner Program.** Today: 1,000 subscribers plus 10M Shorts views in 90 days. **New applicants from 1 Feb 2027** need 20M Shorts views in 90 days, or 8,000 watch hours in 365 days. Reports conflict on the exact Shorts figure, so check YouTube's announcement.
  - YouTube's *reused content* policy rejects "compilations with no original content added". Rolecall's edits help (reframing, captions, titles, credit, tightened pacing), but the safest growth path adds original commentary: an intro hook line or a short text/voice setup.
  - YouTube's Sept 2026 gaming-violence update makes graphic violence in the **first 7 seconds** ineligible for ads. The `early_violence` flag warns about this.
- **TikTok Creator Rewards.**
  - Account: 18+, a supported country, a *personal* account, ~10k followers and 100k views in the last 30 days.
  - Videos: only those **over 1 minute** earn. Turn on the `long` (61-90s) format under Overview → Publishing rules once you're near eligibility.
- **Instagram.** There's no open Reels payout program. Treat Instagram as reach and lead generation for the paid service.
- **Rockstar's video policy.** It allows gameplay videos but describes its intent as "non-commercial". In practice, platform ad revenue on GTA videos is common and tolerated, but Take-Two can remove material at any time. Never sell the footage itself. The paid service sells **editing labor** on footage the streamer owns.
- **RP servers.** Some servers have their own content rules. Ask each streamer to confirm their server allows third-party clipping (the join form's terms cover their own footage).

## 8. Getting paid by streamers (revenue path 2)

- `/p/pricing` collects leads. The default packages are $99 / $199 / $349 per month; change them with `docker compose exec rolecall rolecall set packages '[{"id":"starter","name":"Starter","price":99,"clips":12,"blurb":"..."}]'`.
- When a lead says yes:
  1. They sign `/p/join`, which records permission and auto-verifies their channel code.
  2. You mark them **Paying client** on their Streamers page. Every clip from their footage then also renders without Rolecall branding onto their private delivery page.
  3. Log payments under Metrics → Revenue.
- Payment collection: a **Stripe Payment Link** has no monthly fee (Stripe takes a cut of each payment). Creating the Stripe account is yours to do. Paste the link into your reply emails. I didn't create one, since it's a paid service.

## 9. Your weekly routine once it's running

- **Review queue:** each new streamer's first 3 clips, plus anything flagged. Approve or reject from your phone.
- **TikTok:** post the "Ready for TikTok" clips (up to the daily cap you set).
- **Weekly:** paste TikTok Studio numbers into Metrics, and check *What to double down on*.
- **Alerts:** optional. Set `NOTIFY_WEBHOOK` (Discord webhook or `https://ntfy.sh/<topic>`) to get pinged for reviews, failures and removal requests.

### Sources
- YouTube `videos.insert` (privacy/quota): https://developers.google.com/youtube/v3/docs/videos/insert
- Instagram content publishing: https://developers.facebook.com/docs/instagram-platform/content-publishing/
- TikTok Direct Post + guidelines: https://developers.tiktok.com/doc/content-posting-api-reference-direct-post , https://developers.tiktok.com/doc/content-sharing-guidelines
- YPP changes (Aug 2026): https://www.tubefilter.com/2026/08/10/youtube-partner-program-ad-eligibility-requirements-shorts/
- Shorts monetization / reused content: https://support.google.com/youtube/answer/12504220
- TikTok Creator Rewards summary: https://www.sirency.com/blog/tiktok-creator-rewards-program-eligibility
- YouTube gaming-violence update (Sept 2026): https://www.dexerto.com/gta/youtube-updates-gaming-violence-monetization-rules-ahead-of-gta-6-launch-3405945/
- Rockstar video policy: https://support.rockstargames.com/articles/7bNaeoMFTV0iUDGhStTXvz
