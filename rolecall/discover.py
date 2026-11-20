"""Discovery: find streamers playing GTA 6 and rank them as prospects to invite.

Discovery never clips anything. A prospect becomes clippable only after they opt in at
/p/join?ref=<token> and the ownership code shows up on their channel (pipeline.autoverify); at that
point the stream watcher is switched on for their channel and the rest runs on its own.

Sources
  YouTube  Data API search (live + recently finished streams), using the brand's connected account.
           Costs ~200 of the 10,000 daily quota units per run (an upload costs 1,600).
  Twitch   Helix streams for the "Grand Theft Auto VI" category. Needs TWITCH_CLIENT_ID/SECRET
           (free app at dev.twitch.tv); skipped until set.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import db, events
from .config import get_config

GTA6 = re.compile(r"\bgta\s*-?\s*(6|vi)\b|grand\s+theft\s+auto\s*(6|vi)\b|\bgta6\b", re.I)
# Trailer reactions / news / leaks are mostly Rockstar's footage, not the streamer's gameplay.
LOW_FIT = re.compile(r"\b(trailer|reaction|reacts?|leaks?|news|rumou?rs?|countdown|release date|podcast)\b", re.I)
YT_API = "https://www.googleapis.com/youtube/v3"
TW_API = "https://api.twitch.tv/helix"
_tw_token: dict = {}


# ------------------------------------------------------------------ sources
def youtube_streams(http: httpx.Client | None = None) -> tuple[list[dict], str | None]:
    from .publishers.youtube import YouTube

    yt = YouTube(http)
    ok, why = yt.connected()
    if not ok:
        return [], f"YouTube not connected ({why})"
    query = db.get_setting("discover_youtube_query") or "GTA 6|GTA VI"
    lang = (db.get_setting("discover_languages") or ["en"])[0]
    since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for event, extra in (("live", {"order": "viewCount"}), ("completed", {"order": "viewCount", "publishedAfter": since})):
        res = yt._get(f"{YT_API}/search", {"part": "id", "q": query, "type": "video", "eventType": event,
                                          "videoCategoryId": "20", "maxResults": 25, "relevanceLanguage": lang,
                                          **extra})
        ids += [it["id"]["videoId"] for it in res.get("items", []) if it.get("id", {}).get("videoId")]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return [], None
    videos = []
    for i in range(0, len(ids), 50):
        videos += yt._get(f"{YT_API}/videos", {"part": "snippet,liveStreamingDetails,statistics",
                                               "id": ",".join(ids[i:i + 50])}).get("items", [])
    chan_ids = list(dict.fromkeys(v["snippet"]["channelId"] for v in videos))
    chans = {}
    for i in range(0, len(chan_ids), 50):
        for c in yt._get(f"{YT_API}/channels", {"part": "snippet,statistics",
                                                "id": ",".join(chan_ids[i:i + 50])}).get("items", []):
            chans[c["id"]] = c
    out = []
    for v in videos:
        sn, live = v["snippet"], v.get("liveStreamingDetails") or {}
        ch = chans.get(sn["channelId"], {})
        handle = (ch.get("snippet") or {}).get("customUrl")  # "@name"
        is_live = sn.get("liveBroadcastContent") == "live"
        out.append({
            "platform": "youtube", "channel_id": sn["channelId"], "handle": handle,
            "name": sn.get("channelTitle"), "url": f"https://www.youtube.com/{handle}" if handle
            else f"https://www.youtube.com/channel/{sn['channelId']}",
            "title": sn.get("title", ""), "stream_url": f"https://www.youtube.com/watch?v={v['id']}",
            "viewers": int(live["concurrentViewers"]) if live.get("concurrentViewers") else None,
            "views": int((v.get("statistics") or {}).get("viewCount", 0) or 0),
            "followers": int(((ch.get("statistics") or {}).get("subscriberCount")) or 0) or None,
            "language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"), "live": is_live,
        })
    return out, None


def _twitch_headers(http: httpx.Client) -> dict | None:
    cfg = get_config()
    if not (cfg.twitch_client_id and cfg.twitch_client_secret):
        return None
    if _tw_token.get("expires", 0) < time.time():
        r = http.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": cfg.twitch_client_id, "client_secret": cfg.twitch_client_secret,
            "grant_type": "client_credentials"})
        r.raise_for_status()
        tok = r.json()
        _tw_token.update(token=tok["access_token"], expires=time.time() + tok.get("expires_in", 3600) - 120)
    return {"Client-Id": cfg.twitch_client_id, "Authorization": f"Bearer {_tw_token['token']}"}


def twitch_streams(http: httpx.Client | None = None) -> tuple[list[dict], str | None]:
    http = http or httpx.Client(timeout=20)
    h = _twitch_headers(http)
    if not h:
        return [], "Twitch skipped (add TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET)"
    names = db.get_setting("discover_twitch_games") or ["Grand Theft Auto VI"]
    games = http.get(f"{TW_API}/games", params=[("name", n) for n in names], headers=h).json().get("data", [])
    if not games:
        return [], f"Twitch has no category named {', '.join(names)} yet"
    streams: list[dict] = []
    for lang in db.get_setting("discover_languages") or ["en"]:
        cursor = None
        for _ in range(3):  # up to 300 live streams per language
            params = [("game_id", g["id"]) for g in games] + [("first", "100"), ("language", lang)]
            if cursor:
                params.append(("after", cursor))
            res = http.get(f"{TW_API}/streams", params=params, headers=h).json()
            streams += res.get("data", [])
            cursor = (res.get("pagination") or {}).get("cursor")
            if not cursor:
                break
    return [{
        "platform": "twitch", "channel_id": s["user_id"], "handle": s["user_login"], "name": s["user_name"],
        "url": f"https://www.twitch.tv/{s['user_login']}", "title": s.get("title", ""),
        "stream_url": f"https://www.twitch.tv/{s['user_login']}", "viewers": int(s.get("viewer_count") or 0),
        "views": None, "followers": None, "language": s.get("language"), "live": True, "game": s.get("game_name"),
    } for s in streams], None


# ------------------------------------------------------------------ ranking
def score(peak_viewers: int | None, followers: int | None, views: int | None, days_seen: int,
          language: str | None, title: str) -> float:
    """0..1. Favors mid-size streamers: enough audience and moments to clip, small enough to say yes
    and not already covered by dozens of clip channels."""
    # Audience: live viewers, but a huge subscriber count still means "big" (~1 viewer per 300 subs).
    audience = max(peak_viewers or (views // 20 if views else 0), (followers or 0) // 300)
    lv = math.log10(max(audience, 1))
    size = math.exp(-((lv - 2.9) / 0.85) ** 2)           # peak ~800 concurrent viewers
    consistency = min(1.0, days_seen / 4)                 # streams GTA 6 on several days
    langs = db.get_setting("discover_languages") or ["en"]
    lang_ok = not language or language.split("-")[0].lower() in langs
    fit = (0.5 if LOW_FIT.search(title or "") else 1.0) * (1.0 if lang_ok else 0.45)
    return round((0.7 * size + 0.3 * consistency) * fit, 3)


def rescore_all() -> int:
    rows = db.q("SELECT id, peak_viewers, followers, seen_days, language, last_title FROM prospects")
    for r in rows:
        db.ex("UPDATE prospects SET score=? WHERE id=?", (score(r["peak_viewers"], r["followers"], None,
              len(db.jload(r["seen_days"], [])), r["language"], r["last_title"] or ""), r["id"]))
    return len(rows)


def invite_link(p: dict) -> str:
    cfg = get_config()
    return f"{cfg.public_base_url or cfg.base_url}/p/join?ref={p['invite_token']}"


def invite_text(p: dict) -> str:
    cfg = get_config()
    tpl = db.get_setting("invite_template")
    vals = {"name": p.get("name") or p.get("handle") or "there", "title": (p.get("last_title") or "")[:80],
            "brand": cfg.brand_name, "handle": cfg.brand_handle, "link": invite_link(p)}
    try:
        return tpl.format(**vals)
    except (KeyError, IndexError, ValueError):
        return db.DEFAULT_SETTINGS["invite_template"].format(**vals)


def summary(p) -> dict:
    p = dict(p)
    return {k: p.get(k) for k in ("id", "platform", "handle", "name", "url", "last_title", "last_stream_url",
                                  "peak_viewers", "followers", "seen_count", "score", "status", "language",
                                  "first_seen", "last_seen", "creator_id")} | {
        "days_seen": len(db.jload(p.get("seen_days"), [])), "invite_link": invite_link(p),
        "invite_text": invite_text(p)}


# ------------------------------------------------------------------ store
def _known_creator(url: str) -> int | None:
    from .pipeline import norm_channel

    n = norm_channel(url)
    for c in db.q("SELECT id, primary_url FROM creators WHERE primary_url IS NOT NULL"):
        if norm_channel(c["primary_url"]) == n:
            return c["id"]
    return None


def upsert(found: dict) -> tuple[int, bool]:
    """Insert or refresh one prospect. Returns (id, is_new)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = db.q1("SELECT * FROM prospects WHERE platform=? AND channel_id=?", (found["platform"], found["channel_id"]))
    days = db.jload(row["seen_days"], []) if row else []
    if today not in days:
        days = (days + [today])[-30:]
    peak = max(found.get("viewers") or 0, (row["peak_viewers"] or 0) if row else 0) or None
    followers = found.get("followers") or (row["followers"] if row else None)
    sc = score(peak, followers, found.get("views"), len(days), found.get("language"), found.get("title", ""))
    if row:
        db.ex("UPDATE prospects SET handle=?, name=?, url=?, last_title=?, last_stream_url=?, language=?,"
              " peak_viewers=?, followers=?, seen_count=seen_count+1, seen_days=?, last_seen=?, score=? WHERE id=?",
              (found.get("handle"), found.get("name"), found["url"], found.get("title"), found.get("stream_url"),
               found.get("language"), peak, followers, json.dumps(days), db.now_iso(), sc, row["id"]))
        return row["id"], False
    creator = _known_creator(found["url"])
    pid = db.ex(
        "INSERT INTO prospects(platform, channel_id, handle, name, url, last_title, last_stream_url, language,"
        " peak_viewers, followers, seen_count, seen_days, last_seen, score, status, invite_token, creator_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
        (found["platform"], found["channel_id"], found.get("handle"), found.get("name"), found["url"],
         found.get("title"), found.get("stream_url"), found.get("language"), peak, followers, json.dumps(days),
         db.now_iso(), sc, "joined" if creator else "new", secrets.token_urlsafe(12), creator))
    return pid, True


def discover(youtube_http: httpx.Client | None = None, twitch_http: httpx.Client | None = None) -> dict:
    """Search every source, store/refresh prospects, alert about the best new ones."""
    cfg = get_config()
    result: dict = {"found": 0, "new": 0, "updated": 0, "sources": {}, "notes": [], "top_new": []}
    rescore_all()  # keeps old rows consistent when weights or language settings change
    own = {cfg.brand_handle.lower(), "@" + cfg.brand_handle.lower()}
    for name, fn, http in (("youtube", youtube_streams, youtube_http), ("twitch", twitch_streams, twitch_http)):
        try:
            items, note = fn(http)
        except Exception as e:  # noqa: BLE001 - one source failing must not stop the other
            items, note = [], f"{name} error: {str(e)[:300]}"
            db.log_event("warn", "discover", note)
        if note:
            result["notes"].append(note)
        kept = 0
        for it in items:
            if (it.get("handle") or "").lower() in own:
                continue
            if name == "youtube" and not GTA6.search(it.get("title") or ""):
                continue  # YouTube search is fuzzy; Twitch is filtered by category already
            _, is_new = upsert(it)
            kept += 1
            result["new" if is_new else "updated"] += 1
        result["sources"][name] = kept
        result["found"] += kept
    min_score = float(db.get_setting("discover_min_score") or 0.5)
    top = db.q("SELECT * FROM prospects WHERE status='new' AND notified=0 AND score>=? ORDER BY score DESC LIMIT ?",
               (min_score, int(db.get_setting("discover_notify_top") or 5)))
    if top:
        result["top_new"] = [summary(p) for p in top]
        db.ex(f"UPDATE prospects SET notified=1 WHERE id IN ({','.join('?' * len(top))})", tuple(p["id"] for p in top))
        events.emit("prospects.found", {"count": len(top), "prospects": result["top_new"],
                                        "links": {"prospects": f"{cfg.base_url}/prospects"}})
    db.log_event("info", "discover", f"GTA 6 discovery: {result['found']} streams, {result['new']} new streamers"
                 + (f" ({'; '.join(result['notes'])})" if result["notes"] else ""))
    return result


# ------------------------------------------------------------------ funnel
def mark(prospect_id: int, status: str) -> None:
    if status not in ("new", "invited", "ignored", "declined", "joined"):
        raise ValueError(status)
    db.ex("UPDATE prospects SET status=?, invited_at=CASE WHEN ?='invited' THEN ? ELSE invited_at END WHERE id=?",
          (status, status, db.now_iso(), prospect_id))


def by_token(token: str) -> dict | None:
    row = db.q1("SELECT * FROM prospects WHERE invite_token=?", (token,)) if token else None
    return dict(row) if row else None


def link_join(token: str, creator_id: int) -> None:
    """Called by /p/join: the invited streamer signed up."""
    p = by_token(token)
    if p:
        db.ex("UPDATE prospects SET status='joined', creator_id=? WHERE id=?", (creator_id, p["id"]))
        db.log_event("action", "discover", f"Invited streamer {p['name']} ({p['platform']}) signed up", "creator",
                     creator_id)


def watch_url_for(channel_url: str) -> str | None:
    """Where the stream watcher looks for a streamer's finished streams."""
    from urllib.parse import urlparse

    u = channel_url.strip()
    if "://" not in u:
        u = "https://" + u
    p = urlparse(u)
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    parts = [x for x in p.path.split("/") if x]
    if host == "twitch.tv" and parts:
        return f"https://www.twitch.tv/{parts[0]}/videos?filter=archives"
    if host == "youtube.com" and parts:
        base = "/".join(parts[:2] if parts[0] in ("channel", "c", "user") else parts[:1])
        return f"https://www.youtube.com/{base}/streams"
    return None


def ensure_watch(creator_id: int) -> str | None:
    """Turn on the stream watcher for a newly verified streamer (if not set by hand)."""
    c = db.q1("SELECT watch_url, primary_url FROM creators WHERE id=?", (creator_id,))
    if not c or c["watch_url"] or not c["primary_url"]:
        return c["watch_url"] if c else None
    url = watch_url_for(c["primary_url"])
    if url:
        db.ex("UPDATE creators SET watch_url=? WHERE id=?", (url, creator_id))
        db.log_event("info", "watch", f"Stream watcher on for creator {creator_id}: {url}", "creator", creator_id)
    return url
