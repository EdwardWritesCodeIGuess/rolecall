"""Background worker: ingest, process, publish (pause switch, daily caps, spacing, retries), metrics."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import autopilot, db, events, pipeline, review, watcher
from .config import get_config
from .publishers import PLATFORMS, NotConnected, PostJob, PublishError, get_publisher

log = logging.getLogger(__name__)
BACKOFF_MIN = [5, 15, 60, 240, 720]
_last: dict[str, float] = {}
_stop = threading.Event()


def _due(name: str, every_s: int) -> bool:
    now = time.time()
    if now - _last.get(name, 0) >= every_s:
        _last[name] = now
        return True
    return False


def _utc(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def can_post_now(platform: str, now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now(timezone.utc)
    if db.get_setting("paused"):
        return False, "Publishing is paused"
    if db.get_setting(f"paused_{platform}"):
        return False, f"{platform} is paused"
    if platform not in (db.get_setting("platforms_enabled") or []):
        return False, f"{platform} disabled"
    local = now.astimezone(ZoneInfo(get_config().timezone))
    start, end = int(db.get_setting("post_window_start") or 0), int(db.get_setting("post_window_end") or 24)
    if not (start <= local.hour < end):
        return False, f"Outside posting window {start}:00-{end}:00"
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = db.q1("SELECT COUNT(*) n FROM posts WHERE platform=? AND published_at>=? AND "
              "status IN ('published','export_ready')", (platform, since))["n"]
    limit = int(db.get_setting(f"daily_limit_{platform}") or 0)
    if n >= limit:
        return False, f"Daily limit reached ({n}/{limit})"
    last = db.q1("SELECT MAX(published_at) t FROM posts WHERE platform=? AND status IN ('published','export_ready')",
                 (platform,))["t"]
    gap = int(db.get_setting("min_gap_minutes") or 0)
    if last and now - _utc(last) < timedelta(minutes=gap):
        return False, f"Spacing: last post under {gap} min ago"
    return True, "ok"


def _job(post) -> PostJob:
    clip = db.q1("SELECT * FROM clips WHERE id=?", (post["clip_id"],))
    return PostJob(post_id=post["id"], clip_id=clip["id"], platform=post["platform"], video_path=clip["path"],
                   thumb_path=clip["thumb_path"], copy=db.jload(clip["copy"], {}),
                   idempotency_key=post["idempotency_key"], state=db.jload(post["state"], {}))


def _fail(post, err: str, retryable: bool) -> None:
    attempts = post["attempts"] + 1
    max_attempts = int(db.get_setting("max_attempts") or 5)
    if retryable and attempts < max_attempts:
        wait = BACKOFF_MIN[min(attempts - 1, len(BACKOFF_MIN) - 1)]
        nxt = (datetime.now(timezone.utc) + timedelta(minutes=wait)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.ex("UPDATE posts SET status='queued', attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
              (attempts, nxt, err[:2000], post["id"]))
        db.log_event("warn", "publish", f"Post {post['id']} ({post['platform']}) retry {attempts} in {wait}m: {err}",
                     "post", post["id"])
    else:
        db.ex("UPDATE posts SET status='failed', attempts=?, last_error=? WHERE id=?", (attempts, err[:2000], post["id"]))
        db.log_event("error", "publish", f"Post {post['id']} ({post['platform']}) failed: {err}", "post", post["id"])
        events.emit("post.failed", events.post_summary(post["id"]))


def publish_one(post) -> str:
    """Advance one post's state machine. Returns the new status."""
    clip = db.q1("SELECT * FROM clips WHERE id=?", (post["clip_id"],))
    if clip["is_demo"]:
        db.ex("UPDATE posts SET status='cancelled', last_error='demo footage' WHERE id=?", (post["id"],))
        return "cancelled"
    if clip["review_status"] not in ("approved", "auto_approved"):
        return post["status"]
    flags, allowed = review.permission_flags(clip["creator_id"], [post["platform"]])
    if any(f["severity"] == "block" for f in flags) or post["platform"] not in allowed:
        db.ex("UPDATE posts SET status='cancelled', last_error=? WHERE id=?",
              ("permission no longer covers this post: " + "; ".join(f["detail"] for f in flags), post["id"]))
        db.log_event("action", "publish", f"Post {post['id']} cancelled: permission changed", "post", post["id"])
        return "cancelled"
    pub = get_publisher(post["platform"])
    db.ex("UPDATE posts SET status='uploading' WHERE id=?", (post["id"],))
    try:
        res = pub.publish(_job(post))
    except NotConnected as e:
        db.ex("UPDATE posts SET status='queued', last_error=? WHERE id=?", (str(e), post["id"]))
        return "queued"
    except PublishError as e:
        _fail(post, str(e), e.retryable)
        return "failed"
    except Exception as e:  # noqa: BLE001 - network blips etc. are retryable
        _fail(post, f"{type(e).__name__}: {e}", True)
        return "failed"
    if res.status == "processing":
        db.ex("UPDATE posts SET status='processing', remote_id=? WHERE id=?", (res.remote_id, post["id"]))
        return "processing"
    db.ex("UPDATE posts SET status=?, remote_id=?, remote_url=?, published_at=?, last_error=NULL WHERE id=?",
          (res.status, res.remote_id, res.remote_url, db.now_iso(), post["id"]))
    verb = "published" if res.status == "published" else "ready to post by hand"
    db.log_event("info", "publish", f"Clip {clip['id']} {verb} on {post['platform']}: {res.remote_url or res.remote_id}",
                 "post", post["id"])
    events.emit("post.published" if res.status == "published" else "post.export_ready", events.post_summary(post["id"]))
    return res.status


def publish_due() -> list[dict]:
    """Advance in-flight posts, then publish at most one due post per platform. Returns what changed."""
    now_s = db.now_iso()
    changed: list[dict] = []
    # In-flight posts (resumable uploads, Instagram containers) continue unless paused: the pause
    # switch is a hard stop, so nothing goes public while it is on.
    for post in db.q("SELECT * FROM posts WHERE status IN ('processing','uploading') ORDER BY id"):
        if db.get_setting("paused") or db.get_setting(f"paused_{post['platform']}"):
            continue
        if post["status"] == "uploading" and not db.jload(post["state"], {}):
            db.ex("UPDATE posts SET status='queued' WHERE id=?", (post["id"],))  # crashed before any progress
            continue
        changed.append({"post_id": post["id"], "platform": post["platform"], "status": publish_one(post)})
    for platform in PLATFORMS:
        ok, why = can_post_now(platform)
        if not ok:
            continue
        connected, _ = get_publisher(platform).connected()
        if not connected:
            continue
        post = db.q1(
            "SELECT p.* FROM posts p JOIN clips c ON c.id=p.clip_id WHERE p.platform=? AND p.status='queued' "
            "AND (p.next_attempt_at IS NULL OR p.next_attempt_at<=?) "
            "AND c.review_status IN ('approved','auto_approved') ORDER BY c.score DESC, p.id LIMIT 1",
            (platform, now_s))
        if post:
            changed.append({"post_id": post["id"], "platform": platform, "status": publish_one(post)})
    return changed


def refresh_metrics() -> None:
    for platform in ("youtube", "instagram"):
        pub = get_publisher(platform)
        if not pub.connected()[0]:
            continue
        try:
            f = pub.account_followers()
            if f is not None:
                db.ex("INSERT INTO account_stats(platform, followers) VALUES(?,?)", (platform, f))
        except Exception as e:  # noqa: BLE001
            db.log_event("warn", "metrics", f"{platform} followers: {e}")
        since = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for post in db.q("SELECT * FROM posts WHERE platform=? AND status='published' AND published_at>=?",
                         (platform, since)):
            try:
                m = pub.fetch_metrics(post["remote_id"])
            except Exception as e:  # noqa: BLE001
                db.log_event("warn", "metrics", f"post {post['id']}: {e}")
                continue
            if m:
                db.ex("INSERT INTO post_metrics(post_id, views, watch_time_sec, avg_view_pct, likes, comments, shares,"
                      " followers_gained) VALUES(?,?,?,?,?,?,?,?)",
                      (post["id"], m.get("views"), m.get("watch_time_sec"), m.get("avg_view_pct"), m.get("likes"),
                       m.get("comments"), m.get("shares"), m.get("followers_gained")))


def watch_channels() -> dict:
    return watcher.watch()


def tick() -> None:
    # watch / process / publish: n8n drives these while it is checking in (see autopilot.py); the
    # built-in worker covers them when n8n is not set up or has gone quiet.
    if autopilot.builtin_should_run():
        pipeline.scan_inbox()
        if db.q1("SELECT 1 FROM sources WHERE status='new' LIMIT 1"):
            autopilot.run_inline("process")
        autopilot.run_inline("publish")
        if _due("watch", int(db.get_setting("watch_every_minutes") or 60) * 60):
            autopilot.run_inline("watch")
    if _due("metrics", 6 * 3600):
        refresh_metrics()
    if _due("ig_refresh", 24 * 3600):
        try:
            from .publishers.instagram import Instagram

            Instagram().refresh_if_needed()
        except Exception as e:  # noqa: BLE001
            db.log_event("error", "token", f"Instagram token refresh: {e}")
    if _due("autoverify", 30 * 60):
        try:
            pipeline.autoverify()
        except Exception as e:  # noqa: BLE001
            db.log_event("warn", "permission", f"autoverify: {e}")


def run_forever() -> None:
    interval = get_config().worker_interval
    log.info("worker started (every %ss)", interval)
    while not _stop.is_set():
        try:
            tick()
        except Exception as e:  # noqa: BLE001
            log.exception("tick failed")
            db.log_event("error", "worker", f"{type(e).__name__}: {e}")
        _stop.wait(interval)


def start_background() -> threading.Thread:
    t = threading.Thread(target=run_forever, name="rolecall-worker", daemon=True)
    t.start()
    return t


def stop() -> None:
    _stop.set()


def status_snapshot() -> dict:
    out = {}
    for p in PLATFORMS:
        ok, why = can_post_now(p)
        conn_ok, conn_why = get_publisher(p).connected()
        queued = db.q1("SELECT COUNT(*) n FROM posts WHERE platform=? AND status='queued'", (p,))["n"]
        out[p] = {"can_post": ok, "why": why, "connected": conn_ok, "connection": conn_why, "queued": queued}
    return out


def mark_manual_posted(post_id: int, url: str) -> None:
    """TikTok export mode: record the live URL after posting from the phone."""
    db.ex("UPDATE posts SET status='published', remote_url=?, remote_id=COALESCE(remote_id, ?), "
          "published_at=COALESCE(published_at, ?) WHERE id=?", (url, url, db.now_iso(), post_id))
    db.log_event("info", "publish", f"Post {post_id} marked live: {url}", "post", post_id)


def retry_post(post_id: int) -> None:
    db.ex("UPDATE posts SET status='queued', next_attempt_at=NULL, attempts=0 WHERE id=? AND status='failed'",
          (post_id,))


def debug_dump() -> str:
    return json.dumps(status_snapshot(), indent=2)
