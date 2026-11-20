"""YouTube Shorts via the official YouTube Data API v3 (resumable upload).

A vertical video of 3 minutes or less is classified as a Short automatically.
OAuth scopes: youtube.upload (post), youtube.readonly (duplicate check, subscriber count),
yt-analytics.readonly (watch time / completion). Revoke at https://myaccount.google.com/permissions
"""

from __future__ import annotations

import os
import time
from datetime import date
from urllib.parse import urlencode

import httpx

from ..config import get_config
from ..security import load_token, save_token
from .base import NotConnected, PostJob, PublishError, PublishResult, Publisher

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def redirect_uri() -> str:
    return f"{get_config().base_url}/oauth/youtube/callback"


def auth_url(state: str) -> str:
    cfg = get_config()
    return AUTH_URL + "?" + urlencode({
        "client_id": cfg.google_client_id, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
        "include_granted_scopes": "true",
    })


class YouTube(Publisher):
    platform = "youtube"

    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=httpx.Timeout(60, read=300, write=300))

    # ---- auth -----------------------------------------------------------------
    def exchange_code(self, code: str) -> str:
        cfg = get_config()
        r = self.http.post(TOKEN_URL, data={
            "code": code, "client_id": cfg.google_client_id, "client_secret": cfg.google_client_secret,
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
        if r.status_code != 200:
            raise PublishError(f"Google token exchange failed: {r.text[:300]}", retryable=False)
        tok = r.json()
        if "refresh_token" not in tok:
            raise PublishError("Google returned no refresh token. Remove the app at "
                               "myaccount.google.com/permissions and connect again.", retryable=False)
        tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
        save_token("youtube", tok)
        ch = self._get(f"{API}/channels", {"part": "snippet", "mine": "true"})
        name = (ch.get("items") or [{}])[0].get("snippet", {}).get("title")
        save_token("youtube", tok, account_name=name)
        return name or "connected"

    def _access(self) -> str:
        tok = load_token("youtube")
        if not tok:
            raise NotConnected("youtube")
        if tok.get("expires_at", 0) < time.time():
            cfg = get_config()
            r = self.http.post(TOKEN_URL, data={
                "client_id": cfg.google_client_id, "client_secret": cfg.google_client_secret,
                "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
            if r.status_code != 200:
                retry = r.status_code >= 500
                raise PublishError(f"YouTube token refresh failed ({r.status_code}): {r.text[:200]}. "
                                   "If this says invalid_grant, reconnect YouTube.", retryable=retry)
            new = r.json()
            tok["access_token"] = new["access_token"]
            tok["expires_at"] = time.time() + new.get("expires_in", 3600) - 60
            save_token("youtube", tok)
        return tok["access_token"]

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self._access()}"}

    def _get(self, url: str, params: dict) -> dict:
        r = self.http.get(url, params=params, headers=self._h())
        if r.status_code >= 400:
            raise PublishError(f"YouTube GET {url.rsplit('/', 1)[-1]} {r.status_code}: {r.text[:300]}",
                               retryable=r.status_code >= 500 or r.status_code == 429)
        return r.json()

    def connected(self) -> tuple[bool, str]:
        cfg = get_config()
        if not (cfg.google_client_id and cfg.google_client_secret):
            return False, "Set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET"
        tok = load_token("youtube")
        return (True, "connected") if tok else (False, "Not connected")

    # ---- publish ----------------------------------------------------------------
    def _find_existing(self, marker: str) -> dict | None:
        """Duplicate guard: look for our hidden tag on the channel's latest uploads."""
        ch = self._get(f"{API}/channels", {"part": "contentDetails", "mine": "true"})
        items = ch.get("items") or []
        if not items:
            return None
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        pl = self._get(f"{API}/playlistItems", {"part": "contentDetails", "playlistId": uploads, "maxResults": 25})
        ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
        if not ids:
            return None
        vids = self._get(f"{API}/videos", {"part": "snippet", "id": ",".join(ids)})
        for v in vids.get("items", []):
            if marker in (v.get("snippet", {}).get("tags") or []):
                return v
        return None

    def publish(self, job: PostJob) -> PublishResult:
        size = os.path.getsize(job.video_path)
        marker = f"rc{job.idempotency_key[:10]}"
        upload_url = job.state.get("upload_url")
        if not upload_url and job.state.get("attempted"):
            found = self._find_existing(marker)
            if found:
                return PublishResult(found["id"], f"https://www.youtube.com/shorts/{found['id']}")
        if upload_url:
            # Ask the session where it stands: done (200/201), partial (308), or gone (404/410).
            r = self.http.put(upload_url, headers={**self._h(), "Content-Range": f"bytes */{size}",
                                                   "Content-Length": "0"})
            if r.status_code in (200, 201):
                vid = r.json()["id"]
                return PublishResult(vid, f"https://www.youtube.com/shorts/{vid}")
            if r.status_code == 308:
                rng = r.headers.get("Range")
                offset = int(rng.split("-")[1]) + 1 if rng else 0
                return self._send(job, upload_url, size, offset)
            job.checkpoint(upload_url=None)  # session expired before completion: start a new one
        yt = job.copy.get("youtube", {})
        tags = list(dict.fromkeys((yt.get("tags") or []) + [marker]))
        body = {
            "snippet": {"title": (yt.get("title") or job.copy.get("title") or "GTA RP clip")[:100],
                        "description": yt.get("description", "")[:4900], "tags": tags, "categoryId": "20"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": False},
        }
        job.checkpoint(attempted=True)
        r = self.http.post(UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"}, json=body,
                           headers={**self._h(), "X-Upload-Content-Length": str(size),
                                    "X-Upload-Content-Type": "video/mp4"})
        if r.status_code != 200 or "location" not in r.headers:
            quota = "quota" in r.text.lower()
            raise PublishError(f"YouTube upload init {r.status_code}: {r.text[:300]}",
                               retryable=r.status_code >= 500 or r.status_code == 429 or quota)
        upload_url = r.headers["location"]
        job.checkpoint(upload_url=upload_url)
        return self._send(job, upload_url, size, 0)

    def _send(self, job: PostJob, url: str, size: int, offset: int) -> PublishResult:
        with open(job.video_path, "rb") as f:
            f.seek(offset)
            data = f.read()
        headers = {**self._h(), "Content-Type": "video/mp4", "Content-Length": str(size - offset)}
        if offset:
            headers["Content-Range"] = f"bytes {offset}-{size - 1}/{size}"
        r = self.http.put(url, content=data, headers=headers)
        if r.status_code in (200, 201):
            vid = r.json()["id"]
            return PublishResult(vid, f"https://www.youtube.com/shorts/{vid}")
        raise PublishError(f"YouTube upload {r.status_code}: {r.text[:300]}",
                           retryable=r.status_code >= 500 or r.status_code in (308, 429))

    # ---- metrics ----------------------------------------------------------------
    def fetch_metrics(self, remote_id: str) -> dict | None:
        data = self._get(ANALYTICS, {
            "ids": "channel==MINE", "startDate": "2020-01-01", "endDate": date.today().isoformat(),
            "metrics": "views,estimatedMinutesWatched,averageViewPercentage,likes,comments,shares,subscribersGained",
            "filters": f"video=={remote_id}"})
        rows = data.get("rows") or []
        if not rows:
            return {"views": 0}
        v = rows[0]
        return {"views": v[0], "watch_time_sec": v[1] * 60, "avg_view_pct": v[2], "likes": v[3],
                "comments": v[4], "shares": v[5], "followers_gained": v[6]}

    def account_followers(self) -> int | None:
        ch = self._get(f"{API}/channels", {"part": "statistics", "mine": "true"})
        items = ch.get("items") or []
        return int(items[0]["statistics"].get("subscriberCount", 0)) if items else None
