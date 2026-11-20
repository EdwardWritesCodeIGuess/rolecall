"""Stream watcher: finds new, finished VODs on authorized streamers' channels and ingests them.

Each creator can have a `watch_url` (e.g. https://www.twitch.tv/<name>/videos?filter=archives or a
YouTube channel's /streams page). Only creators with an active, verified permission are watched, and
ingest_url re-checks that every VOD sits on a covered channel. Streams still in progress are skipped
and picked up on a later run once the VOD is complete.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from . import db, events, pipeline

LIVE_STATES = {"is_live", "is_upcoming", "post_live"}
MAX_ERRORS = 3


def _entries(watch_url: str, limit: int) -> list[dict]:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": limit}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(watch_url, download=False) or {}
    if "entries" in info:
        return [e for e in (info.get("entries") or []) if e][:limit]
    return [info]


def _entry_url(e: dict) -> str | None:
    url = e.get("webpage_url") or e.get("url")
    return url if url and url.startswith("http") else None


def _entry_time(e: dict) -> float | None:
    for k in ("timestamp", "release_timestamp"):
        if e.get(k):
            return float(e[k])
    d = e.get("upload_date")
    if d and len(d) == 8:
        return datetime.strptime(d, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
    return None


def _seen(url: str) -> dict | None:
    row = db.q1("SELECT * FROM watch_seen WHERE url=?", (url,))
    return dict(row) if row else None


def _mark(url: str, creator_id: int, status: str, reason: str | None = None) -> None:
    db.ex("INSERT INTO watch_seen(url, creator_id, status, reason, attempts) VALUES(?,?,?,?,?) "
          "ON CONFLICT(url) DO UPDATE SET status=excluded.status, reason=excluded.reason, "
          "attempts=watch_seen.attempts + (excluded.status='error'), seen_at=CURRENT_TIMESTAMP",
          (url, creator_id, status, (reason or "")[:500], int(status == "error")))


def candidates(creator: dict, now: float | None = None) -> list[dict]:
    """New, finished, recent VODs for one creator, newest first, not yet ingested or ruled out."""
    now = now or time.time()
    limit = int(db.get_setting("watch_lookback") or 5)
    max_age = float(db.get_setting("watch_max_age_days") or 14) * 86400
    out = []
    for e in _entries(creator["watch_url"], limit):
        url = _entry_url(e)
        if not url or db.q1("SELECT 1 FROM sources WHERE origin=?", (url,)):
            continue
        seen = _seen(url)
        if seen and (seen["status"] in ("ingested", "skipped") or seen["attempts"] >= MAX_ERRORS):
            continue
        if e.get("is_live") or e.get("live_status") in LIVE_STATES:
            _mark(url, creator["id"], "live", "stream still in progress")
            continue
        ts = _entry_time(e)
        if ts and now - ts > max_age:
            _mark(url, creator["id"], "skipped", "older than watch_max_age_days")
            continue
        dur = e.get("duration")
        if dur and dur < 20:
            _mark(url, creator["id"], "skipped", "shorter than 20s")
            continue
        out.append({"url": url, "title": e.get("title"), "duration": dur, "timestamp": ts})
    return out


def watch(creator_slug: str | None = None, per_creator: int | None = None) -> dict:
    """Check every watched channel (or one), ingest up to `per_creator` new VODs each, scan the inbox."""
    per = per_creator or int(db.get_setting("watch_per_run") or 1)
    sql = "SELECT * FROM creators WHERE watch_url IS NOT NULL AND watch_url!=''"
    rows = db.q(sql + " AND slug=?", (creator_slug,)) if creator_slug else db.q(sql)
    result: dict = {"checked": [], "ingested": [], "waiting": [], "skipped": [], "errors": []}
    for c in rows:
        c = dict(c)
        if not pipeline.active_permission(c["id"]):
            result["skipped"].append({"creator": c["slug"], "reason": "no active, verified permission"})
            continue
        result["checked"].append(c["slug"])
        try:
            cands = candidates(c)
        except Exception as e:  # noqa: BLE001 - one bad channel must not stop the rest
            result["errors"].append({"creator": c["slug"], "error": str(e)[:300]})
            db.log_event("warn", "watch", f"{c['slug']}: {e}", "creator", c["id"])
            continue
        n = 0
        for v in cands:
            if n >= per:
                break
            try:
                sid = pipeline.ingest_url(c["id"], v["url"], kind="watch")
            except pipeline.StillLive as e:
                _mark(v["url"], c["id"], "live", str(e))
                result["waiting"].append({"creator": c["slug"], "url": v["url"]})
                continue
            except pipeline.PermissionDenied as e:
                _mark(v["url"], c["id"], "skipped", str(e))
                result["skipped"].append({"creator": c["slug"], "url": v["url"], "reason": str(e)[:300]})
                db.log_event("warn", "watch", f"{c['slug']}: {e}", "creator", c["id"])
                continue
            except Exception as e:  # noqa: BLE001
                _mark(v["url"], c["id"], "error", str(e))
                result["errors"].append({"creator": c["slug"], "url": v["url"], "error": str(e)[:300]})
                db.log_event("warn", "watch", f"{c['slug']} {v['url']}: {e}", "creator", c["id"])
                continue
            _mark(v["url"], c["id"], "ingested")
            n += 1
            item = {"creator": c["slug"], "source_id": sid, "url": v["url"], "title": v["title"]}
            result["ingested"].append(item)
            events.emit("source.ingested", item)
    result["inbox"] = pipeline.scan_inbox()
    return result
