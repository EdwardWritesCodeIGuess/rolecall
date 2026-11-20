"""Outbound events for automation (n8n). Fire-and-forget JSON POSTs; never blocks or raises.

Events: source.ingested, clip.ready, clip.needs_review, post.published, post.export_ready,
post.failed, job.finished, job.failed. n8n's "Rolecall events" workflow turns them into alerts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading

import httpx

from . import db
from .config import get_config

log = logging.getLogger(__name__)


def _send(url: str, body: dict, secret: bytes) -> None:
    raw = json.dumps(body, default=str).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    try:
        httpx.post(url, content=raw, timeout=10,
                   headers={"Content-Type": "application/json", "X-Rolecall-Signature": f"sha256={sig}"})
    except httpx.HTTPError as e:
        log.warning("event webhook failed (%s): %s", body.get("event"), e)


def emit(event: str, payload: dict) -> None:
    cfg = get_config()
    if not cfg.n8n_webhook_url:
        return
    from .security import api_token  # local import: security imports db

    body = {"event": event, "ts": db.now_iso(), "brand": cfg.brand_handle, **payload}
    threading.Thread(target=_send, args=(cfg.n8n_webhook_url, body, api_token().encode()), daemon=True).start()


def clip_summary(clip_id: int) -> dict:
    """Everything a human (or n8n) needs to judge a clip without opening the dashboard."""
    cfg = get_config()
    c = db.q1("SELECT c.*, cr.display_name, cr.slug, cr.primary_url FROM clips c JOIN creators cr ON cr.id=c.creator_id "
              "WHERE c.id=?", (clip_id,))
    if not c:
        return {"clip_id": clip_id}
    copy = db.jload(c["copy"], {})
    flags = db.jload(c["flags"], [])
    return {
        "clip_id": c["id"], "source_id": c["source_id"],
        "creator": {"slug": c["slug"], "name": c["display_name"], "channel": c["primary_url"]},
        "credit": copy.get("credit"),
        "title": c["title"], "format": c["fmt"], "layout": c["layout"],
        "duration": round(c["duration"] or 0, 1), "score": c["score"],
        "review_status": c["review_status"], "is_demo": bool(c["is_demo"]),
        "flags": flags,
        "needs_human": [f for f in flags if f["severity"] in ("block", "review")],
        "copy": {k: v for k, v in copy.items() if k in ("youtube", "instagram", "tiktok")},
        "links": {"review": f"{cfg.base_url}/clips/{c['id']}", "posts": f"{cfg.base_url}/posts"},
    }


def post_summary(post_id: int) -> dict:
    cfg = get_config()
    p = db.q1("SELECT * FROM posts WHERE id=?", (post_id,))
    if not p:
        return {"post_id": post_id}
    out = {"post_id": p["id"], "platform": p["platform"], "mode": p["mode"], "status": p["status"],
           "remote_url": p["remote_url"], "error": p["last_error"], "attempts": p["attempts"],
           "links": {"posts": f"{cfg.base_url}/posts?status={p['status']}",
                     "video": f"{cfg.base_url}/posts/{p['id']}/video"}}
    out["clip"] = clip_summary(p["clip_id"])
    return out
