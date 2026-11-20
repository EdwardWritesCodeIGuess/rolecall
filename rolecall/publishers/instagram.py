"""Instagram Reels via the official Instagram API with Instagram Login (no Facebook Page needed).

Needs an Instagram professional (creator/business) account added to your Meta app as a tester, a
token with instagram_business_basic + instagram_business_content_publish (+ ..._manage_insights for
metrics), and PUBLIC_BASE_URL: Instagram downloads the video from a URL, so the homelab must expose
a short-lived signed link over HTTPS (Tailscale Funnel or a Cloudflare Tunnel; see docs/LAUNCH.md).
Revoke at Instagram > Settings > Apps and websites.
"""

from __future__ import annotations

import os
import shutil
import time

import httpx

from .. import db
from ..config import get_config
from ..security import load_token, save_token, sign
from .base import NotConnected, PostJob, PublishError, PublishResult, Publisher

VERSION = os.environ.get("IG_API_VERSION", "v24.0")
GRAPH = f"https://graph.instagram.com/{VERSION}"


class Instagram(Publisher):
    platform = "instagram"

    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=60)

    def connect_with_token(self, token: str) -> str:
        r = self.http.get(f"{GRAPH}/me", params={"fields": "user_id,username,account_type", "access_token": token})
        if r.status_code != 200:
            raise PublishError(f"Instagram rejected the token: {r.text[:300]}", retryable=False)
        me = r.json()
        save_token("instagram", {"access_token": token, "ig_id": me.get("user_id") or me.get("id"),
                                 "created": time.time()}, account_name=me.get("username"))
        return me.get("username", "connected")

    def refresh_if_needed(self) -> None:
        """Long-lived tokens last 60 days; refresh weekly (must be at least 24h old)."""
        tok = load_token("instagram")
        if not tok or time.time() - tok.get("created", 0) < 7 * 86400:
            return
        r = self.http.get("https://graph.instagram.com/refresh_access_token",
                          params={"grant_type": "ig_refresh_token", "access_token": tok["access_token"]})
        if r.status_code == 200:
            tok["access_token"] = r.json()["access_token"]
            tok["created"] = time.time()
            save_token("instagram", tok)
        else:
            raise PublishError(f"Instagram token refresh failed: {r.text[:200]}", retryable=False)

    def _tok(self) -> dict:
        tok = load_token("instagram")
        if not tok:
            raise NotConnected("instagram")
        return tok

    def mode(self) -> str:
        return db.get_setting("instagram_mode") or "api"

    def connected(self) -> tuple[bool, str]:
        if self.mode() == "export":
            return True, "Export mode: post by hand at instagram.com"
        if not get_config().public_base_url:
            return False, "Set PUBLIC_BASE_URL (HTTPS) so Instagram can fetch videos"
        return (True, "connected") if load_token("instagram") else (False, "Not connected")

    def _call(self, method: str, path: str, **params) -> dict:
        tok = self._tok()
        params["access_token"] = tok["access_token"]
        r = self.http.request(method, f"{GRAPH}/{path}", params=params if method == "GET" else None,
                              data=params if method == "POST" else None)
        if r.status_code >= 400:
            err = r.json().get("error", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
            code = err.get("code")
            retry = r.status_code >= 500 or code in (4, 17, 32, 613, 2, 1)  # rate limits / transient
            raise PublishError(f"Instagram {path.split('/')[-1]} {r.status_code}: {err.get('message') or r.text[:300]}",
                               retryable=retry)
        return r.json()

    def media_url(self, job: PostJob) -> str:
        base = get_config().public_base_url
        if not base.startswith("https://"):
            raise PublishError("PUBLIC_BASE_URL must be an https:// URL Instagram can reach.", retryable=False)
        path = f"/m/{job.post_id}/clip.mp4"
        return f"{base}{path}?sig={sign(path, ttl=6 * 3600)}"

    def _find_by_caption(self, ig_id: str, caption: str) -> dict | None:
        res = self._call("GET", f"{ig_id}/media", fields="id,caption,permalink,timestamp", limit=15)
        for m in res.get("data", []):
            if (m.get("caption") or "").strip()[:120] == caption.strip()[:120]:
                return m
        return None

    def export(self, job: PostJob) -> PublishResult:
        out = get_config().dir("exports", "instagram")
        dst = out / f"clip{job.clip_id:05d}.mp4"
        if not dst.exists():
            shutil.copy2(job.video_path, dst)
        (out / f"clip{job.clip_id:05d}.txt").write_text(job.copy.get("instagram", {}).get("caption", ""))
        return PublishResult(f"export:{dst.name}", None, status="export_ready")

    def publish(self, job: PostJob) -> PublishResult:
        if self.mode() == "export":
            return self.export(job)
        tok = self._tok()
        ig_id = tok["ig_id"]
        caption = job.copy.get("instagram", {}).get("caption", job.copy.get("title", ""))[:2200]
        if job.state.get("media_id"):
            m = self._call("GET", job.state["media_id"], fields="permalink")
            return PublishResult(job.state["media_id"], m.get("permalink"))
        container = job.state.get("container_id")
        if not container:
            if job.state.get("attempted"):
                found = self._find_by_caption(ig_id, caption)
                if found:
                    return PublishResult(found["id"], found.get("permalink"))
            job.checkpoint(attempted=True)
            res = self._call("POST", f"{ig_id}/media", media_type="REELS", video_url=self.media_url(job),
                             caption=caption, share_to_feed="true")
            container = res["id"]
            job.checkpoint(container_id=container, container_at=time.time())
            return PublishResult(container, None, status="processing")
        st = self._call("GET", container, fields="status_code,status")
        code = st.get("status_code")
        if code == "IN_PROGRESS":
            if time.time() - job.state.get("container_at", time.time()) > 30 * 60:
                job.checkpoint(container_id=None)
                raise PublishError("Instagram processing took over 30 minutes; will retry.", retryable=True)
            return PublishResult(container, None, status="processing")
        if code in ("ERROR", "EXPIRED"):
            job.checkpoint(container_id=None)
            raise PublishError(f"Instagram container {code}: {st.get('status', '')}", retryable=code == "EXPIRED")
        if code == "PUBLISHED":
            found = self._find_by_caption(ig_id, caption)
            if found:
                job.checkpoint(media_id=found["id"])
                return PublishResult(found["id"], found.get("permalink"))
        # FINISHED: publish exactly once per container
        res = self._call("POST", f"{ig_id}/media_publish", creation_id=container)
        media_id = res["id"]
        job.checkpoint(media_id=media_id)
        m = self._call("GET", media_id, fields="permalink")
        return PublishResult(media_id, m.get("permalink"))

    def fetch_metrics(self, remote_id: str) -> dict | None:
        res = self._call("GET", f"{remote_id}/insights",
                         metric="views,likes,comments,shares,saved,ig_reels_avg_watch_time,ig_reels_video_view_total_time")
        vals = {d["name"]: (d.get("values") or [{}])[0].get("value", 0) for d in res.get("data", [])}
        return {"views": vals.get("views"), "likes": vals.get("likes"), "comments": vals.get("comments"),
                "shares": vals.get("shares"),
                "watch_time_sec": (vals.get("ig_reels_video_view_total_time") or 0) / 1000,
                "avg_watch_sec": (vals.get("ig_reels_avg_watch_time") or 0) / 1000}

    def account_followers(self) -> int | None:
        tok = self._tok()
        return self._call("GET", str(tok["ig_id"]), fields="followers_count").get("followers_count")
