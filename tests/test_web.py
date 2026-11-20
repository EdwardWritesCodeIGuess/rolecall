"""Dashboard auth/CSRF and the public streamer flows."""

import pytest
from fastapi.testclient import TestClient

from rolecall import db, pipeline, security
from rolecall.config import get_config

from .conftest import make_clip, make_creator


@pytest.fixture()
def client(env):
    env.worker_enabled = False
    from rolecall.web.app import app, csrf_token

    with TestClient(app) as c:
        c.csrf = csrf_token()
        yield c


def test_admin_requires_auth(client):
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("admin", "wrong")).status_code == 401
    assert client.get("/", auth=("admin", "pw")).status_code == 200


def test_post_requires_csrf(client):
    assert client.post("/pause", auth=("admin", "pw"), data={}).status_code == 403
    r = client.post("/pause", auth=("admin", "pw"), data={"csrf": client.csrf}, follow_redirects=False)
    assert r.status_code == 303 and db.get_setting("paused") is True


def test_all_admin_pages_render(client, tmp_path):
    cid = make_creator()
    clip = make_clip(cid, tmp_path / "a.mp4", review_status="needs_review")
    for path in ["/", "/clips?status=needs_review", f"/clips/{clip}", "/posts", "/sources", "/creators",
                 f"/creators/{cid}", "/metrics", "/leads", "/requests", "/connections", "/events"]:
        r = client.get(path, auth=("admin", "pw"))
        assert r.status_code == 200, path


def test_join_flow_records_pending_permission_and_verification(client, monkeypatch):
    r = client.post("/p/join", data={
        "display_name": "Officer Bob", "email": "bob@example.test", "channel_url": "https://www.twitch.tv/officerbob",
        "p_youtube": "on", "p_tiktok": "on", "allow_edit": "on", "allow_repost": "on", "allow_monetize": "on",
        "revenue_share": "pct", "revenue_share_pct": "20", "agree": "on", "signature": "Bob Example",
        "want_service": "on"})
    assert r.status_code == 200 and "RC-" in r.text
    c = db.q1("SELECT * FROM creators WHERE slug='officer-bob'")
    p = db.q1("SELECT * FROM permissions WHERE creator_id=?", (c["id"],))
    assert p["status"] == "pending" and p["verified"] == 0 and p["revenue_share_pct"] == 20
    assert "submission" in p["evidence"]
    assert db.q1("SELECT COUNT(*) n FROM leads")["n"] == 1
    with pytest.raises(pipeline.PermissionDenied):
        pipeline.ingest_file(c["id"], "/nonexistent.mp4")

    class Resp:
        text = f"<meta property='og:description' content='RP streamer {c['verify_code']}'>"

    monkeypatch.setattr("httpx.get", lambda *a, **k: Resp())
    assert pipeline.autoverify() == 1
    p = db.q1("SELECT * FROM permissions WHERE creator_id=?", (c["id"],))
    assert p["verified"] == 1 and p["status"] == "active"


def test_join_requires_agreement(client):
    r = client.post("/p/join", data={"display_name": "X", "email": "x@example.test", "channel_url": "https://x.test",
                                     "signature": "X"})
    assert "Missing" in r.text
    assert db.q1("SELECT COUNT(*) n FROM permissions")["n"] == 0


def test_removal_request_cancels_posts(client, tmp_path):
    cid = make_creator(channels=("https://www.twitch.tv/streamer",))
    clip = make_clip(cid, tmp_path / "a.mp4")
    pipeline.queue_posts(clip)
    r = client.post("/p/remove", data={"name": "S", "email": "s@example.test",
                                       "channel_url": "twitch.tv/streamer/", "urls": "", "reason": "no"})
    assert r.status_code == 200
    assert db.q1("SELECT COUNT(*) n FROM posts WHERE status!='cancelled'")["n"] == 0
    assert db.q1("SELECT creator_id FROM removal_requests")["creator_id"] == cid


def test_signed_media_url(client, tmp_path):
    cid = make_creator()
    clip = make_clip(cid, tmp_path / "a.mp4")
    pid = pipeline.queue_posts(clip)[0]
    path = f"/m/{pid}/clip.mp4"
    assert client.get(path + "?sig=bad").status_code == 403
    assert client.get(f"{path}?sig={security.sign(path)}").status_code == 200
    db.ex("UPDATE posts SET status='published' WHERE id=?", (pid,))
    assert client.get(f"{path}?sig={security.sign(path)}").status_code == 404, "link dies once published"


def test_client_delivery_page(client, tmp_path):
    cid = make_creator()
    clip = make_clip(cid, tmp_path / "a.mp4")
    (tmp_path / "d.mp4").write_bytes(b"x")
    db.ex("UPDATE clips SET delivery_path=? WHERE id=?", (str(tmp_path / "d.mp4"), clip))
    db.ex("UPDATE creators SET is_client=1, delivery_token='tok_abcdefghijklmnop' WHERE id=?", (cid,))
    assert client.get("/d/tok_abcdefghijklmnop").status_code == 200
    assert client.get(f"/d/tok_abcdefghijklmnop/{clip}.mp4").status_code == 200
    assert client.get("/d/wrong_token_123456789").status_code == 404


def test_approve_blocked_for_demo(client, tmp_path):
    cid = make_creator()
    clip = make_clip(cid, tmp_path / "a.mp4", review_status="needs_review", is_demo=1)
    db.ex("UPDATE clips SET flags=? WHERE id=?",
          ('[{"code":"demo_footage","severity":"block","detail":"demo"}]', clip))
    r = client.post(f"/clips/{clip}/approve", auth=("admin", "pw"), data={"csrf": client.csrf})
    assert "approve this clip" in r.text
    assert db.q1("SELECT COUNT(*) n FROM posts")["n"] == 0


def test_oauth_callback_is_state_protected_and_single_use(client):
    r = client.get("/oauth/youtube/callback?code=abc&state=forged")
    assert r.status_code == 200 and "not connected" in r.text
    r = client.get("/connect/youtube", auth=("admin", "pw"), follow_redirects=False)
    assert r.status_code == 307 and "accounts.google.com" in r.headers["location"]
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    from rolecall.web import app as webapp
    assert webapp._check_state("youtube", state) is True
    assert webapp._check_state("youtube", state) is False, "state must be single-use"
