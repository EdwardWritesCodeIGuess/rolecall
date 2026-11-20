"""Machine API for n8n and other automation. Bearer-token auth (ROLECALL_API_TOKEN or data/.api_token).

GET  /api/v1/status                 pause state, who is driving, per-platform readiness, queue counts
GET  /api/v1/targets                where to post: brand accounts, modes, limits, posting window
GET  /api/v1/creators               authorized streamers, their channels, credit lines, permissions
POST /api/v1/run/{stage}            watch | process | publish  (?wait=<seconds> to block for the result)
GET  /api/v1/jobs/{id}              a stage run's status/result
GET  /api/v1/clips?status=...       clips with flags, copy and review links
GET  /api/v1/clips/{id}
POST /api/v1/clips/{id}/approve     optional JSON {"overrides": {"youtube.title": "..."}}
POST /api/v1/clips/{id}/reject
GET  /api/v1/posts?status=...       e.g. export_ready = waiting to be posted by hand
POST /api/v1/posts/{id}/posted      JSON {"url": "https://..."} after a manual post
POST /api/v1/pause                  JSON {"paused": true|false, "platform": optional}
GET  /api/v1/report?days=1          daily/weekly numbers for the report workflow
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import autopilot, db, events, pipeline, review, scheduler, security, stats
from ..config import get_config
from ..publishers import PLATFORMS, get_publisher

PROFILE_URLS = {"youtube": "https://www.youtube.com/@{h}", "instagram": "https://www.instagram.com/{h}/",
                "tiktok": "https://www.tiktok.com/@{h}"}
MAX_WAIT = 3300  # keep under common proxy/n8n timeouts (n8n nodes here use 3600s)


def require_token(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token.encode(), security.api_token().encode()):
        raise HTTPException(401, "Bad or missing API token", headers={"WWW-Authenticate": "Bearer"})


api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])


# ------------------------------------------------------------------ status + targets
@api.get("/status")
def status() -> dict:
    k = stats.overview()
    return {
        "brand": get_config().brand_name, "handle": get_config().brand_handle,
        "paused": bool(db.get_setting("paused")),
        "driver": autopilot.driver(), "orchestrator": db.get_setting("orchestrator"),
        "platforms": scheduler.status_snapshot(),
        "running": autopilot.running(),
        "counts": {key: k[key] for key in ("needs_review", "queued", "export_ready", "failed", "published_7d",
                                           "published_total", "clips_total", "sources_processing")},
    }


@api.get("/targets")
def targets() -> dict:
    """Where Rolecall posts, read from its settings and connected accounts."""
    cfg = get_config()
    enabled = db.get_setting("platforms_enabled") or []
    accounts = {r["platform"]: r["account_name"] for r in db.q("SELECT platform, account_name FROM tokens")}
    out = []
    for p in PLATFORMS:
        pub = get_publisher(p)
        connected, why = pub.connected()
        mode = "api"
        if p in ("instagram", "tiktok"):
            mode = db.get_setting(f"{p}_mode") or ("export" if p == "tiktok" else "api")
        can, reason = scheduler.can_post_now(p)
        out.append({
            "platform": p, "enabled": p in enabled, "mode": mode,
            "posting": "automatic" if mode == "api" else "manual (export: video + caption prepared for you)",
            "connected": connected, "connection": why, "account": accounts.get(p),
            "profile_url": PROFILE_URLS[p].format(h=cfg.brand_handle),
            "paused": bool(db.get_setting(f"paused_{p}")),
            "daily_limit": db.get_setting(f"daily_limit_{p}"), "can_post_now": can, "reason": reason,
        })
    return {
        "brand": cfg.brand_name, "handle": cfg.brand_handle, "timezone": cfg.timezone,
        "window": {"start_hour": db.get_setting("post_window_start"), "end_hour": db.get_setting("post_window_end")},
        "min_gap_minutes": db.get_setting("min_gap_minutes"),
        "formats": db.get_setting("formats"),
        "platforms": out,
    }


@api.get("/creators")
def creators() -> list[dict]:
    out = []
    for c in db.q("SELECT * FROM creators WHERE slug!='demo' ORDER BY display_name"):
        perm = review.permission_for(c["id"])
        out.append({
            "slug": c["slug"], "name": c["display_name"], "channel": c["primary_url"], "watch_url": c["watch_url"],
            "credit": pipeline.credit_for(dict(c), perm), "is_client": bool(c["is_client"]),
            "auto_approve": bool(c["auto_approve"]),
            "permission": None if not perm else {
                "status": perm["status"], "verified": bool(perm["verified"]),
                "usable": bool(pipeline.active_permission(c["id"])),
                "channels": db.jload(perm["source_channels"], []), "platforms": db.jload(perm["platforms"], []),
                "edit": bool(perm["allow_edit"]), "repost": bool(perm["allow_repost"]),
                "monetize": bool(perm["allow_monetize"]), "expires_at": perm["expires_at"],
            },
        })
    return out


# ------------------------------------------------------------------ stages
@api.post("/run/{stage}")
def run_stage(stage: str, wait: int = 0, creator: str | None = None, source_id: int | None = None):
    if stage not in autopilot.STAGES:
        raise HTTPException(404, f"Unknown stage '{stage}'. Use one of: {', '.join(autopilot.STAGES)}")
    autopilot.note_orchestrator_call()
    if db.get_setting("paused") and stage != "publish":
        # publish handles pause itself (in-flight uploads stop too); watch/process just wait
        return {"status": "paused", "stage": stage, "message": "Rolecall is paused"}
    kw = {"creator": creator} if stage == "watch" else {"source_id": source_id} if stage == "process" else {}
    try:
        job = autopilot.start(stage, by="api", **kw)
    except autopilot.Busy as b:
        return JSONResponse({"status": "busy", "stage": stage, "running": autopilot.public(b.job)}, status_code=409)
    if wait > 0:
        job["_done"].wait(min(wait, MAX_WAIT))
    return autopilot.public(job)


@api.get("/jobs/{job_id}")
def job(job_id: str) -> dict:
    j = autopilot.get_job(job_id)
    if not j:
        raise HTTPException(404, "Unknown job (jobs are kept in memory; a restart clears them)")
    return autopilot.public(j)


# ------------------------------------------------------------------ clips + posts
@api.get("/clips")
def clips(status: str = "", limit: int = 20) -> list[dict]:
    where = "WHERE review_status=? AND is_demo=0" if status else "WHERE is_demo=0"
    rows = db.q(f"SELECT id FROM clips {where} ORDER BY id DESC LIMIT ?",
                ((status,) if status else ()) + (max(1, min(limit, 100)),))
    return [events.clip_summary(r["id"]) for r in rows]


@api.get("/clips/{cid}")
def clip(cid: int) -> dict:
    if not db.q1("SELECT 1 FROM clips WHERE id=?", (cid,)):
        raise HTTPException(404)
    return events.clip_summary(cid)


@api.post("/clips/{cid}/approve")
def clip_approve(cid: int, body: dict | None = Body(default=None)) -> dict:
    if not db.q1("SELECT 1 FROM clips WHERE id=?", (cid,)):
        raise HTTPException(404)
    try:
        posts = pipeline.approve(cid, (body or {}).get("overrides"))
    except pipeline.PermissionDenied as e:
        raise HTTPException(409, str(e)) from e
    db.log_event("info", "review", f"Clip {cid} approved via API", "clip", cid)
    return {"clip_id": cid, "review_status": "approved", "queued_posts": posts}


@api.post("/clips/{cid}/reject")
def clip_reject(cid: int) -> dict:
    if not db.q1("SELECT 1 FROM clips WHERE id=?", (cid,)):
        raise HTTPException(404)
    pipeline.reject(cid)
    db.log_event("info", "review", f"Clip {cid} rejected via API", "clip", cid)
    return {"clip_id": cid, "review_status": "rejected"}


@api.get("/posts")
def posts(status: str = "", limit: int = 20) -> list[dict]:
    where = "WHERE status=?" if status else ""
    rows = db.q(f"SELECT id FROM posts {where} ORDER BY id DESC LIMIT ?",
                ((status,) if status else ()) + (max(1, min(limit, 100)),))
    return [events.post_summary(r["id"]) for r in rows]


@api.post("/posts/{pid}/posted")
def post_posted(pid: int, body: dict = Body(...)) -> dict:
    url = str(body.get("url", "")).strip()
    if not url.startswith("https://"):
        raise HTTPException(422, "Send the live post's https:// URL")
    if not db.q1("SELECT 1 FROM posts WHERE id=?", (pid,)):
        raise HTTPException(404)
    scheduler.mark_manual_posted(pid, url)
    return events.post_summary(pid)


@api.post("/pause")
def pause(body: dict = Body(...)) -> dict:
    plat = body.get("platform")
    if plat and plat not in PLATFORMS:
        raise HTTPException(422, f"platform must be one of {PLATFORMS}")
    key = f"paused_{plat}" if plat else "paused"
    db.set_setting(key, bool(body.get("paused", True)))
    db.log_event("action", "pause", f"{key} -> {db.get_setting(key)} (API)")
    return {key: db.get_setting(key)}


@api.get("/report")
def report(days: int = 1) -> dict:
    days = max(1, min(days, 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    since_iso = since.replace(" ", "T") + "Z"
    one = lambda sql, *a: db.q1(sql, a)[0] or 0  # noqa: E731
    k = stats.overview()
    by_platform = {r["platform"]: r["n"] for r in db.q(
        "SELECT platform, COUNT(*) n FROM posts WHERE status='published' AND published_at>=? GROUP BY platform",
        (since_iso,))}
    top = [dict(r) for r in db.q(
        "SELECT c.id clip_id, c.title, p.platform, p.remote_url, MAX(m.views) views FROM post_metrics m "
        "JOIN posts p ON p.id=m.post_id JOIN clips c ON c.id=p.clip_id WHERE p.published_at>=? "
        "GROUP BY p.id ORDER BY views DESC LIMIT 3", (since_iso,))]
    return {
        "days": days,
        "sources_ingested": one("SELECT COUNT(*) FROM sources WHERE is_demo=0 AND created_at>=?", since),
        "clips_made": one("SELECT COUNT(*) FROM clips WHERE is_demo=0 AND created_at>=?", since),
        "published": sum(by_platform.values()), "published_by_platform": by_platform,
        "needs_review": k["needs_review"], "export_ready": k["export_ready"], "failed": k["failed"],
        "followers": k["followers"], "views_total": k["views"],
        "revenue_month": k["revenue_month"], "cost_month": k["cost_month"], "cost_per_clip": k["cost_per_clip"],
        "leads_new": k["leads_new"], "clients": k["clients"],
        "top": top, "driver": autopilot.driver(), "paused": bool(db.get_setting("paused")),
        "dashboard": get_config().base_url,
    }
