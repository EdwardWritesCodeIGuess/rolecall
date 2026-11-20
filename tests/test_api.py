"""Machine API for n8n, the stage runner, stream watcher, events and the new review checks."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from rolecall import autopilot, copywriter, db, events, pipeline, review, security, watcher

from .conftest import make_clip, make_creator


@pytest.fixture()
def api(env):
    env.worker_enabled = False
    from rolecall.web.app import app

    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {security.api_token()}"})
        yield c


def test_api_requires_token(env):
    env.worker_enabled = False
    from rolecall.web.app import app

    with TestClient(app) as c:
        assert c.get("/api/v1/status").status_code == 401
        assert c.get("/api/v1/status", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/api/v1/status", auth=("admin", "pw")).status_code == 401, "dashboard login is not an API token"


def test_targets_reads_where_to_post(api):
    t = api.get("/api/v1/targets").json()
    assert t["handle"] == "rolecallclips"
    by = {p["platform"]: p for p in t["platforms"]}
    assert by["youtube"]["enabled"] and by["youtube"]["mode"] == "api"
    assert by["youtube"]["profile_url"] == "https://www.youtube.com/@rolecallclips"
    assert by["instagram"]["mode"] == "export" and by["instagram"]["posting"].startswith("manual")
    assert not by["tiktok"]["enabled"]


def test_creators_lists_credit_and_permission(api):
    make_creator("bigrp")
    [c] = api.get("/api/v1/creators").json()
    assert c["slug"] == "bigrp" and c["credit"] == "twitch.tv/bigrp"
    assert c["permission"]["usable"] and c["permission"]["monetize"]


def test_run_stage_wait_returns_result_and_marks_n8n_driving(api, monkeypatch):
    cid = make_creator()
    sid = db.ex("INSERT INTO sources(creator_id, kind, path, duration, sha256) VALUES(?,?,?,?,?)",
                (cid, "url", "/x.mp4", 60, "s1"))
    seen = []
    monkeypatch.setattr(pipeline, "process_source", lambda s: seen.append(s) or [])
    assert autopilot.builtin_should_run(), "no n8n yet: built-in worker drives"
    r = api.post("/api/v1/run/process?wait=30").json()
    assert r["status"] == "done" and r["result"]["source_id"] == sid and seen == [sid]
    assert not autopilot.builtin_should_run(), "n8n just called: it drives"
    later = datetime.now(timezone.utc) + timedelta(minutes=120)
    assert autopilot.builtin_should_run(later), "n8n quiet past the grace period: worker takes over"
    job = api.get(f"/api/v1/jobs/{r['id']}").json()
    assert job["status"] == "done"
    assert api.post("/api/v1/run/nope").status_code == 404


def test_heavy_stages_never_overlap(api, monkeypatch):
    autopilot._locks["heavy"].acquire()
    try:
        r = api.post("/api/v1/run/watch")
        assert r.status_code == 409 and r.json()["status"] == "busy"
        assert api.post("/api/v1/run/publish?wait=10").json()["status"] == "done", "posting is not blocked"
    finally:
        autopilot._locks["heavy"].release()


def test_paused_skips_heavy_stages(api):
    db.set_setting("paused", True)
    assert api.post("/api/v1/run/watch").json()["status"] == "paused"


def test_stage_errors_are_reported_not_raised(api, monkeypatch):
    def boom(**_):
        raise RuntimeError("disk full")
    monkeypatch.setitem(autopilot.STAGES, "watch", (boom, "heavy"))
    r = api.post("/api/v1/run/watch?wait=10").json()
    assert r["status"] == "error" and "disk full" in r["error"]
    assert not autopilot._locks["heavy"].locked()


def test_review_through_api(api, tmp_path):
    cid = make_creator()
    clip = make_clip(cid, tmp_path / "a.mp4", review_status="needs_review")
    lst = api.get("/api/v1/clips?status=needs_review").json()
    assert [c["clip_id"] for c in lst] == [clip] and lst[0]["links"]["review"].endswith(f"/clips/{clip}")
    r = api.post(f"/api/v1/clips/{clip}/approve", json={"overrides": {"youtube.title": "Better title"}}).json()
    assert r["review_status"] == "approved" and len(r["queued_posts"]) == 2  # youtube + instagram enabled
    assert db.q1("SELECT title FROM clips WHERE id=?", (clip,))["title"] == "Better title"
    clip2 = make_clip(cid, tmp_path / "b.mp4", review_status="needs_review")
    assert api.post(f"/api/v1/clips/{clip2}/reject").json()["review_status"] == "rejected"


def test_manual_post_handoff(api, tmp_path):
    clip = make_clip(make_creator(), tmp_path / "a.mp4")
    [_, ig] = pipeline.queue_posts(clip)
    assert db.q1("SELECT mode FROM posts WHERE id=?", (ig,))["mode"] == "export"
    assert api.post(f"/api/v1/posts/{ig}/posted", json={"url": "ftp://x"}).status_code == 422
    r = api.post(f"/api/v1/posts/{ig}/posted", json={"url": "https://www.instagram.com/reel/abc/"}).json()
    assert r["status"] == "published" and r["remote_url"].endswith("/abc/")


def test_report(api):
    r = api.get("/api/v1/report?days=7").json()
    assert r["days"] == 7 and "published_by_platform" in r and r["driver"] in ("n8n", "builtin worker")


def test_events_are_signed_json(env, monkeypatch):
    env.n8n_webhook_url = "http://n8n.test/webhook/rolecall-events"
    sent = []

    class Inline:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(events.threading, "Thread", Inline)
    monkeypatch.setattr(events.httpx, "post", lambda url, content, timeout, headers: sent.append((url, content, headers)))
    events.emit("clip.needs_review", {"clip_id": 7})
    [(url, raw, headers)] = sent
    body = json.loads(raw)
    assert url.endswith("/rolecall-events") and body["event"] == "clip.needs_review" and body["clip_id"] == 7
    import hashlib
    import hmac
    good = hmac.new(security.api_token().encode(), raw, hashlib.sha256).hexdigest()
    assert headers["X-Rolecall-Signature"] == f"sha256={good}"


def test_events_disabled_without_webhook(env, monkeypatch):
    monkeypatch.setattr(events.threading, "Thread", lambda **_: pytest.fail("should not send"))
    events.emit("clip.ready", {})


# ------------------------------------------------------------------ stream watcher
def test_watcher_ingests_only_finished_recent_vods(env, monkeypatch):
    cid = make_creator()
    db.ex("UPDATE creators SET watch_url=? WHERE id=?", ("https://www.twitch.tv/streamer/videos?filter=archives", cid))
    now = datetime.now(timezone.utc).timestamp()
    entries = [
        {"url": "https://www.twitch.tv/videos/5", "live_status": "is_live", "title": "live now"},
        {"url": "https://www.twitch.tv/videos/4", "timestamp": now - 3600, "duration": 14000, "title": "last night"},
        {"url": "https://www.twitch.tv/videos/3", "timestamp": now - 3 * 86400, "duration": 9000, "title": "older"},
        {"url": "https://www.twitch.tv/videos/1", "timestamp": now - 60 * 86400, "duration": 9000, "title": "ancient"},
    ]
    monkeypatch.setattr(watcher, "_entries", lambda url, limit: entries)
    got = []
    monkeypatch.setattr(pipeline, "ingest_url", lambda c, u, kind: got.append(u) or len(got))
    r = watcher.watch()
    assert got == ["https://www.twitch.tv/videos/4"], "one new finished VOD per channel per run"
    assert r["ingested"][0]["source_id"] == 1
    statuses = {row["url"]: row["status"] for row in db.q("SELECT url, status FROM watch_seen")}
    assert statuses["https://www.twitch.tv/videos/5"] == "live"
    assert statuses["https://www.twitch.tv/videos/1"] == "skipped"
    watcher.watch()
    assert got[-1] == "https://www.twitch.tv/videos/3", "next run takes the next VOD"


def test_watcher_skips_unverified_creators_and_waits_on_live(env, monkeypatch):
    unverified = make_creator("newbie", verified=False)
    ok = make_creator("ok")
    for c in (unverified, ok):
        db.ex("UPDATE creators SET watch_url='https://www.twitch.tv/x/videos' WHERE id=?", (c,))
    monkeypatch.setattr(watcher, "_entries", lambda url, limit: [{"url": "https://www.twitch.tv/videos/9"}])

    def still_live(c, u, kind):
        raise pipeline.StillLive("live")
    monkeypatch.setattr(pipeline, "ingest_url", still_live)
    r = watcher.watch()
    assert r["checked"] == ["ok"] and r["skipped"][0]["creator"] == "newbie"
    assert r["waiting"] and not r["ingested"]
    assert db.q1("SELECT status FROM watch_seen")["status"] == "live", "live VODs are retried later"


# ------------------------------------------------------------------ credits + on-screen PII
def test_ensure_credit_on_every_platform():
    copy = {"title": "t", "youtube": {"title": "t", "description": "Funny cop moment"},
            "instagram": {"caption": "lol"}, "tiktok": {"caption": "lol #gtarp"}}
    out = copywriter.ensure_credit(copy, "twitch.tv/bigrp")
    assert out["credit"] == "twitch.tv/bigrp"
    for text in (out["youtube"]["description"], out["instagram"]["caption"], out["tiktok"]["caption"]):
        assert "twitch.tv/bigrp" in text
    again = copywriter.ensure_credit(out, "twitch.tv/bigrp")
    assert again["instagram"]["caption"].count("twitch.tv/bigrp") == 1, "never duplicated"


def test_screen_text_hits_ignore_in_game_numbers():
    assert review.screen_text_hits("Call me (555) 555-0142 for a tow") == []
    assert review.screen_text_hits("Officer 555-0199 responding") == []
    labels = [h[0] for h in review.screen_text_hits("Discord: jake.smith@gmail.com  call 404-867-5309")]
    assert "email address" in labels and "phone number" in labels
    assert review.screen_text_hits("version 1.2.3.400") == [], "not an IP: 400 > 255"


def test_screen_text_flags_without_tesseract(monkeypatch, tmp_path):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    [f] = review.screen_text_flags(tmp_path / "x.mp4", 20, tmp_path / "w")
    assert f["code"] == "screen_text_unchecked" and f["severity"] == "info"
