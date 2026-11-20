"""Publisher state machines against mocked platform APIs (no real accounts touched)."""

import json
import time

import httpx
import pytest

from rolecall import db, security
from rolecall.publishers import PostJob, PublishError
from rolecall.publishers.instagram import Instagram
from rolecall.publishers.tiktok import TikTok
from rolecall.publishers.youtube import YouTube

from .conftest import make_clip, make_creator


def job_for(clip_id, platform, tmp_path, state=None):
    pid = db.ex("INSERT INTO posts(clip_id, platform, idempotency_key, state) VALUES(?,?,?,?)",
                (clip_id, platform, f"key{clip_id}{platform}", json.dumps(state or {})))
    post = db.q1("SELECT * FROM posts WHERE id=?", (pid,))
    clip = db.q1("SELECT * FROM clips WHERE id=?", (clip_id,))
    return PostJob(pid, clip_id, platform, clip["path"], None, json.loads(clip["copy"]), post["idempotency_key"],
                   json.loads(post["state"]))


class YTFake:
    def __init__(self, existing_tag=None, partial=False):
        self.calls = []
        self.existing_tag = existing_tag
        self.partial = partial
        self.uploaded = None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        self.calls.append((req.method, u.split("?")[0]))
        if "oauth2.googleapis.com/token" in u:
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        if req.method == "POST" and "upload/youtube/v3/videos" in u:
            body = json.loads(req.content)
            assert body["status"]["privacyStatus"] == "public"
            assert body["snippet"]["categoryId"] == "20"
            return httpx.Response(200, headers={"location": "https://upload.example/session1"})
        if req.method == "PUT" and "session1" in u:
            if req.headers.get("content-range", "").startswith("bytes */"):
                if self.partial:
                    return httpx.Response(308, headers={"Range": "bytes=0-1023"})
                return httpx.Response(404)
            self.uploaded = req.headers.get("content-range") or "full"
            return httpx.Response(200, json={"id": "VID123"})
        if "/channels" in u:
            return httpx.Response(200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}},
                                                        "snippet": {"title": "Rolecall"}}]})
        if "/playlistItems" in u:
            return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": "OLD1"}}]})
        if "/videos" in u:
            tags = [self.existing_tag] if self.existing_tag else ["other"]
            return httpx.Response(200, json={"items": [{"id": "OLD1", "snippet": {"tags": tags}}]})
        return httpx.Response(500, text=f"unexpected {u}")


@pytest.fixture()
def yt_connected(env):
    security.save_token("youtube", {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3000})


def test_youtube_fresh_upload(env, yt_connected, tmp_path):
    clip = make_clip(make_creator(), tmp_path / "a.mp4")
    fake = YTFake()
    res = YouTube(httpx.Client(transport=httpx.MockTransport(fake))).publish(job_for(clip, "youtube", tmp_path))
    assert res.remote_id == "VID123" and res.remote_url.endswith("/shorts/VID123")
    state = json.loads(db.q1("SELECT state FROM posts").__getitem__("state"))
    assert state["upload_url"] == "https://upload.example/session1", "upload session must be checkpointed"


def test_youtube_resumes_partial_upload_instead_of_reuploading(env, yt_connected, tmp_path):
    clip = make_clip(make_creator(), tmp_path / "a.mp4")
    fake = YTFake(partial=True)
    job = job_for(clip, "youtube", tmp_path, state={"attempted": True, "upload_url": "https://upload.example/session1"})
    res = YouTube(httpx.Client(transport=httpx.MockTransport(fake))).publish(job)
    assert res.remote_id == "VID123"
    assert fake.uploaded.startswith("bytes 1024-"), "must resume from the server's offset"
    assert not any(m == "POST" for m, _ in fake.calls), "must not create a second upload session"


def test_youtube_duplicate_guard_finds_existing_video(env, yt_connected, tmp_path):
    clip = make_clip(make_creator(), tmp_path / "a.mp4")
    job = job_for(clip, "youtube", tmp_path, state={"attempted": True})
    fake = YTFake(existing_tag=f"rc{job.idempotency_key[:10]}")
    res = YouTube(httpx.Client(transport=httpx.MockTransport(fake))).publish(job)
    assert res.remote_id == "OLD1"
    assert not any(m in ("POST", "PUT") for m, _ in fake.calls), "already uploaded: no new upload"


def test_youtube_refreshes_expired_token(env, tmp_path):
    security.save_token("youtube", {"access_token": "old", "refresh_token": "r", "expires_at": 0})
    clip = make_clip(make_creator(), tmp_path / "a.mp4")
    fake = YTFake()
    YouTube(httpx.Client(transport=httpx.MockTransport(fake))).publish(job_for(clip, "youtube", tmp_path))
    assert security.load_token("youtube")["access_token"] == "new"


class IGFake:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.published = 0
        self.video_url = None

    def __call__(self, req):
        u = str(req.url)
        if req.method == "POST" and u.endswith("/17841/media"):
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            self.video_url = httpx.QueryParams(req.content.decode()).get("video_url")
            assert form["media_type"] == "REELS"
            return httpx.Response(200, json={"id": "CONT1"})
        if req.method == "GET" and "/CONT1" in u:
            return httpx.Response(200, json={"status_code": self.statuses.pop(0)})
        if req.method == "POST" and u.endswith("/media_publish"):
            self.published += 1
            return httpx.Response(200, json={"id": "MEDIA9"})
        if req.method == "GET" and "/MEDIA9" in u:
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc/"})
        if req.method == "GET" and "/17841/media" in u:
            return httpx.Response(200, json={"data": [{"id": "MEDIA9", "caption": "Caption for a.mp4",
                                                       "permalink": "https://www.instagram.com/reel/abc/"}]})
        return httpx.Response(500, text=u)


def test_instagram_container_flow_publishes_once(env, tmp_path):
    db.set_setting("instagram_mode", "api")  # default is export (manual posting)
    security.save_token("instagram", {"access_token": "t", "ig_id": "17841", "created": time.time()})
    clip = make_clip(make_creator(), tmp_path / "a.mp4")
    fake = IGFake(["IN_PROGRESS", "FINISHED"])
    ig = Instagram(httpx.Client(transport=httpx.MockTransport(fake)))
    job = job_for(clip, "instagram", tmp_path)
    r1 = ig.publish(job)
    assert r1.status == "processing"
    assert fake.video_url.startswith("https://media.example.test/m/") and "sig=" in fake.video_url
    path = "/m/" + fake.video_url.split("/m/")[1].split("?")[0]
    assert security.verify(path, fake.video_url.split("sig=")[1]), "signed media link must verify"
    assert ig.publish(job).status == "processing"
    r3 = ig.publish(job)
    assert r3.status == "published" and r3.remote_url.endswith("/reel/abc/")
    assert fake.published == 1


def test_instagram_recovers_when_publish_response_was_lost(env, tmp_path):
    db.set_setting("instagram_mode", "api")  # default is export (manual posting)
    security.save_token("instagram", {"access_token": "t", "ig_id": "17841", "created": time.time()})
    clip = make_clip(make_creator(), tmp_path / "a.mp4")
    fake = IGFake(["PUBLISHED"])
    ig = Instagram(httpx.Client(transport=httpx.MockTransport(fake)))
    job = job_for(clip, "instagram", tmp_path, state={"attempted": True, "container_id": "CONT1",
                                                       "container_at": time.time()})
    res = ig.publish(job)
    assert res.remote_id == "MEDIA9" and fake.published == 0, "must not publish the same container twice"


def test_instagram_requires_https_public_url(env, tmp_path):
    db.set_setting("instagram_mode", "api")  # default is export (manual posting)
    env.public_base_url = "http://192.168.1.5:8080"
    security.save_token("instagram", {"access_token": "t", "ig_id": "17841", "created": time.time()})
    clip = make_clip(make_creator(), tmp_path / "a.mp4")
    with pytest.raises(PublishError) as e:
        Instagram(httpx.Client(transport=httpx.MockTransport(IGFake([])))).publish(job_for(clip, "instagram", tmp_path))
    assert not e.value.retryable


def test_tiktok_export_mode_prepares_files(env, tmp_path):
    clip = make_clip(make_creator(), tmp_path / "a.mp4")
    res = TikTok().publish(job_for(clip, "tiktok", tmp_path))
    assert res.status == "export_ready"
    out = env.data_dir / "exports" / "tiktok"
    assert (out / f"clip{clip:05d}.mp4").exists() and (out / f"clip{clip:05d}.txt").read_text() == "tt caption"


def test_instagram_export_mode_prepares_files(env, tmp_path):
    db.set_setting("instagram_mode", "export")
    clip = make_clip(make_creator(), tmp_path / "a.mp4")
    ig = Instagram()
    assert ig.connected()[0]
    res = ig.publish(job_for(clip, "instagram", tmp_path))
    assert res.status == "export_ready"
    out = env.data_dir / "exports" / "instagram"
    assert (out / f"clip{clip:05d}.txt").read_text() == "Caption for a.mp4"
