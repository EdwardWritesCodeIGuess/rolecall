"""TikTok.

TikTok's Content Posting API only allows public Direct Posts from *audited* apps, and its guidelines
reject tools that only post to accounts you or your team manage. So the default is EXPORT mode:
the finished video + caption land in the dashboard (and data/exports/tiktok) for a 30-second manual
post from the phone. Optional INBOX mode sends the video to the TikTok app's inbox as a draft
(scope video.upload, needs an approved TikTok developer app); you tap the notification to post.
Revoke at TikTok > Settings > Security > Manage app permissions.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .. import db
from ..config import get_config
from ..security import load_token, save_token
from .base import NotConnected, PostJob, PublishError, PublishResult, Publisher

API = "https://open.tiktokapis.com/v2"
AUTH = "https://www.tiktok.com/v2/auth/authorize/"
CHUNK = 10 * 1024 * 1024


def redirect_uri() -> str:
    cfg = get_config()
    return f"{cfg.public_base_url or cfg.base_url}/oauth/tiktok/callback"


def auth_url(state: str) -> str:
    return AUTH + "?" + urlencode({"client_key": get_config().tiktok_client_key, "response_type": "code",
                                   "scope": "user.info.basic,video.upload", "redirect_uri": redirect_uri(),
                                   "state": state})


class TikTok(Publisher):
    platform = "tiktok"

    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=httpx.Timeout(60, write=300))

    def mode(self) -> str:
        return db.get_setting("tiktok_mode") or "export"

    def connected(self) -> tuple[bool, str]:
        if self.mode() == "export":
            return True, "Export mode: videos + captions are prepared for manual posting"
        return (True, "Inbox mode connected") if load_token("tiktok") else (False, "Inbox mode: not connected")

    # ---- auth (inbox mode only) -----------------------------------------------------
    def exchange_code(self, code: str) -> str:
        cfg = get_config()
        r = self.http.post(f"{API}/oauth/token/", data={
            "client_key": cfg.tiktok_client_key, "client_secret": cfg.tiktok_client_secret, "code": code,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
        tok = r.json()
        if r.status_code != 200 or "access_token" not in tok:
            raise PublishError(f"TikTok token exchange failed: {r.text[:300]}", retryable=False)
        tok["expires_at"] = time.time() + tok.get("expires_in", 86400) - 120
        save_token("tiktok", tok, account_name=tok.get("open_id"))
        return "connected"

    def _access(self) -> str:
        tok = load_token("tiktok")
        if not tok:
            raise NotConnected("tiktok", "inbox mode needs a connected TikTok developer app")
        if tok.get("expires_at", 0) < time.time():
            cfg = get_config()
            r = self.http.post(f"{API}/oauth/token/", data={
                "client_key": cfg.tiktok_client_key, "client_secret": cfg.tiktok_client_secret,
                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
            new = r.json()
            if r.status_code != 200 or "access_token" not in new:
                raise PublishError(f"TikTok refresh failed: {r.text[:200]}", retryable=False)
            tok.update(new)
            tok["expires_at"] = time.time() + new.get("expires_in", 86400) - 120
            save_token("tiktok", tok)
        return tok["access_token"]

    # ---- publish -------------------------------------------------------------------
    def export(self, job: PostJob) -> PublishResult:
        out = get_config().dir("exports", "tiktok")
        dst = out / f"clip{job.clip_id:05d}.mp4"
        if not dst.exists():
            shutil.copy2(job.video_path, dst)
        (out / f"clip{job.clip_id:05d}.txt").write_text(job.copy.get("tiktok", {}).get("caption", ""))
        return PublishResult(f"export:{dst.name}", None, status="export_ready")

    def publish(self, job: PostJob) -> PublishResult:
        if self.mode() != "inbox":
            return self.export(job)
        h = {"Authorization": f"Bearer {self._access()}", "Content-Type": "application/json; charset=UTF-8"}
        publish_id = job.state.get("publish_id")
        if not publish_id:
            size = os.path.getsize(job.video_path)
            chunk = size if size <= 64 * 1024 * 1024 else CHUNK
            count = max(1, size // chunk)
            r = self.http.post(f"{API}/post/publish/inbox/video/init/", headers=h, json={
                "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                                "total_chunk_count": count}})
            body = r.json()
            if r.status_code != 200 or body.get("error", {}).get("code", "ok") != "ok":
                err = body.get("error", {})
                raise PublishError(f"TikTok init {r.status_code}: {err.get('code')} {err.get('message')}",
                                   retryable=err.get("code") in ("rate_limit_exceeded", "internal_error"))
            publish_id = body["data"]["publish_id"]
            job.checkpoint(publish_id=publish_id)
            self._upload(job.video_path, body["data"]["upload_url"], size, chunk, count)
        r = self.http.post(f"{API}/post/publish/status/fetch/", headers=h, json={"publish_id": publish_id})
        data = r.json().get("data", {})
        status = data.get("status")
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            return PublishResult(publish_id, None, status="export_ready")
        if status == "FAILED":
            job.checkpoint(publish_id=None)
            raise PublishError(f"TikTok processing failed: {data.get('fail_reason')}", retryable=True)
        return PublishResult(publish_id, None, status="processing")

    def _upload(self, path: str, url: str, size: int, chunk: int, count: int) -> None:
        with open(path, "rb") as f:
            for i in range(count):
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1
                f.seek(start)
                data = f.read(end - start + 1)
                r = self.http.put(url, content=data, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {start}-{end}/{size}"})
                if r.status_code not in (200, 201, 206):
                    raise PublishError(f"TikTok upload chunk {i + 1}/{count}: {r.status_code} {r.text[:200]}")


def export_dir() -> Path:
    return get_config().dir("exports", "tiktok")
