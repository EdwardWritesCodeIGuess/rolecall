"""GTA 6 discovery: YouTube/Twitch parsing, ranking, alerts, invite funnel, auto-watch on verify."""

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from rolecall import autopilot, db, discover, events, pipeline, security

from .conftest import make_creator


def yt_fake(videos):
    """videos: list of (video_id, channel_id, title, live_viewers|None, channel_handle, subs)."""
    def handler(req):
        u, p = str(req.url), dict(req.url.params)
        if "/search" in u:
            return httpx.Response(200, json={"items": [{"id": {"videoId": v[0]}} for v in videos]})
        if "/videos" in u:
            want = p["id"].split(",")
            return httpx.Response(200, json={"items": [{
                "id": v[0], "snippet": {"channelId": v[1], "channelTitle": v[4].lstrip("@").title(), "title": v[2],
                                        "liveBroadcastContent": "live" if v[3] else "none",
                                        "defaultAudioLanguage": "en"},
                "liveStreamingDetails": {"concurrentViewers": str(v[3])} if v[3] else {},
                "statistics": {"viewCount": "12000"}} for v in videos if v[0] in want]})
        if "/channels" in u:
            return httpx.Response(200, json={"items": [{"id": v[1], "snippet": {"customUrl": v[4]},
                                                        "statistics": {"subscriberCount": str(v[5])}} for v in videos]})
        return httpx.Response(500, text=u)
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture()
def yt(env):
    security.save_token("youtube", {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 3600})


def test_youtube_discovery_keeps_gta6_streams_and_skips_own_channel(yt, env):
    client = yt_fake([
        ("v1", "UC1", "GTA 6 first heist with the boys!", 900, "@heistguy", 40000),
        ("v2", "UC2", "Minecraft hardcore day 40", 3000, "@blocky", 90000),
        ("v3", "UC3", "GTA VI clips compilation", 50, "@rolecallclips", 10),
        ("v4", "UC4", "Grand Theft Auto VI trailer reaction", 5000, "@newsy", 900000),
    ])
    r = discover.discover(youtube_http=client)
    assert r["sources"]["youtube"] == 2 and r["new"] == 2
    rows = {p["handle"]: dict(p) for p in db.q("SELECT * FROM prospects")}
    assert set(rows) == {"@heistguy", "@newsy"}, "non-GTA6 and our own channel are dropped"
    assert rows["@heistguy"]["url"] == "https://www.youtube.com/@heistguy" and rows["@heistguy"]["peak_viewers"] == 900
    assert rows["@heistguy"]["score"] > rows["@newsy"]["score"], "gameplay beats trailer reactions"
    assert "Twitch skipped" in " ".join(r["notes"])


def test_rediscovery_updates_instead_of_duplicating(yt, env):
    vids = [("v1", "UC1", "GTA 6 cop RP", 400, "@copper", 5000)]
    discover.discover(youtube_http=yt_fake(vids))
    vids2 = [("v9", "UC1", "GTA 6 cop RP day 2", 1200, "@copper", 5100)]
    r = discover.discover(youtube_http=yt_fake(vids2))
    assert r["new"] == 0 and r["updated"] == 1
    p = db.q1("SELECT * FROM prospects")
    assert p["seen_count"] == 2 and p["peak_viewers"] == 1200 and p["last_title"] == "GTA 6 cop RP day 2"


def test_twitch_discovery(env, monkeypatch):
    env.twitch_client_id, env.twitch_client_secret = "cid", "sec"
    discover._tw_token.clear()

    def handler(req):
        u = str(req.url)
        if "oauth2/token" in u:
            return httpx.Response(200, json={"access_token": "app", "expires_in": 3600})
        assert req.headers["Client-Id"] == "cid" and req.headers["Authorization"] == "Bearer app"
        if "/games" in u:
            return httpx.Response(200, json={"data": [{"id": "999", "name": "Grand Theft Auto VI"}]})
        if "/streams" in u:
            return httpx.Response(200, json={"data": [
                {"user_id": "1", "user_login": "midsize", "user_name": "MidSize", "title": "first day in Leonida",
                 "viewer_count": 700, "language": "en", "game_name": "Grand Theft Auto VI"},
                {"user_id": "2", "user_login": "megastar", "user_name": "MegaStar", "title": "GTA 6!!",
                 "viewer_count": 120000, "language": "en", "game_name": "Grand Theft Auto VI"},
                {"user_id": "3", "user_login": "tiny", "user_name": "Tiny", "title": "chill",
                 "viewer_count": 2, "language": "en", "game_name": "Grand Theft Auto VI"},
            ], "pagination": {}})
        return httpx.Response(500, text=u)
    r = discover.discover(twitch_http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert r["sources"]["twitch"] == 3
    scores = {p["handle"]: p["score"] for p in db.q("SELECT handle, score FROM prospects")}
    assert scores["midsize"] > scores["megastar"] and scores["midsize"] > scores["tiny"]
    assert db.q1("SELECT url FROM prospects WHERE handle='midsize'")["url"] == "https://www.twitch.tv/midsize"


def test_alerts_once_about_best_new_prospects(yt, env, monkeypatch):
    sent = []
    monkeypatch.setattr(events, "emit", lambda ev, payload: sent.append((ev, payload)))
    vids = [("v1", "UC1", "GTA 6 chaos", 800, "@chaos", 20000)]
    discover.discover(youtube_http=yt_fake(vids))
    discover.discover(youtube_http=yt_fake(vids))
    found = [p for ev, p in sent if ev == "prospects.found"]
    assert len(found) == 1, "each prospect is alerted about once"
    [p] = found[0]["prospects"]
    assert p["name"] == "Chaos" and "/p/join?ref=" in p["invite_link"] and p["invite_link"] in p["invite_text"]


def test_existing_creator_is_marked_joined(yt, env):
    cid = make_creator("heistguy", channels=("https://www.youtube.com/@heistguy",))
    discover.discover(youtube_http=yt_fake([("v1", "UC1", "GTA 6 heist", 900, "@heistguy", 4000)]))
    p = db.q1("SELECT status, creator_id FROM prospects")
    assert p["status"] == "joined" and p["creator_id"] == cid


@pytest.mark.parametrize("url,watch", [
    ("https://www.twitch.tv/MidSize", "https://www.twitch.tv/MidSize/videos?filter=archives"),
    ("twitch.tv/midsize/about", "https://www.twitch.tv/midsize/videos?filter=archives"),
    ("https://www.youtube.com/@heistguy", "https://www.youtube.com/@heistguy/streams"),
    ("https://youtube.com/channel/UC123/featured", "https://www.youtube.com/channel/UC123/streams"),
    ("https://kick.com/someone", None),
])
def test_watch_url_for(url, watch):
    assert discover.watch_url_for(url) == watch


# ------------------------------------------------------------------ funnel through the web app
@pytest.fixture()
def client(env):
    env.worker_enabled = False
    from rolecall.web.app import app, csrf_token

    with TestClient(app) as c:
        c.csrf = csrf_token()
        yield c


def test_invite_link_prefills_join_and_links_the_signup(client, yt, env):
    discover.discover(youtube_http=yt_fake([("v1", "UC1", "GTA 6 heist", 900, "@heistguy", 4000)]))
    p = dict(db.q1("SELECT * FROM prospects"))
    page = client.get(f"/p/join?ref={p['invite_token']}").text
    assert 'value="https://www.youtube.com/@heistguy"' in page and f'value="{p["invite_token"]}"' in page
    r = client.post("/p/join", data={"display_name": "Heistguy", "email": "h@example.com", "signature": "H G",
                                     "channel_url": p["url"], "agree": "on", "allow_edit": "on", "allow_repost": "on",
                                     "allow_monetize": "on", "p_youtube": "on", "ref": p["invite_token"]})
    assert r.status_code == 200
    after = db.q1("SELECT status, creator_id FROM prospects WHERE id=?", (p["id"],))
    creator = db.q1("SELECT * FROM creators WHERE id=?", (after["creator_id"],))
    assert after["status"] == "joined" and creator["slug"] == "heistguy"
    assert not pipeline.active_permission(creator["id"]), "joining alone is not permission: ownership must verify"


def test_verifying_permission_turns_on_the_stream_watcher(client, env):
    cid = make_creator("midsize", verified=False, channels=("https://www.twitch.tv/midsize",))
    pid = db.q1("SELECT id FROM permissions WHERE creator_id=?", (cid,))["id"]
    r = client.post(f"/permissions/{pid}/verify", auth=("admin", "pw"), data={"csrf": client.csrf},
                    follow_redirects=False)
    assert r.status_code == 303
    assert db.q1("SELECT watch_url FROM creators WHERE id=?", (cid,))["watch_url"] == \
        "https://www.twitch.tv/midsize/videos?filter=archives"


def test_autoverify_turns_on_the_stream_watcher(env, monkeypatch):
    cid = db.ex("INSERT INTO creators(slug, display_name, primary_url, verify_code) VALUES(?,?,?,?)",
                ("yt", "YT", "https://www.youtube.com/@ytguy", "RC-ABC123"))
    db.ex("INSERT INTO permissions(creator_id, status, verified, source_channels, platforms, allow_edit, allow_repost,"
          " allow_monetize) VALUES(?,?,?,?,?,1,1,1)", (cid, "pending", 0, '["https://www.youtube.com/@ytguy"]',
                                                       '["youtube"]'))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, text="bio: RC-ABC123"))
    assert pipeline.autoverify() == 1
    assert db.q1("SELECT watch_url FROM creators WHERE id=?", (cid,))["watch_url"] == \
        "https://www.youtube.com/@ytguy/streams"


def test_prospects_page_and_api(client, yt, env):
    discover.discover(youtube_http=yt_fake([("v1", "UC1", "GTA 6 heist", 900, "@heistguy", 4000)]))
    page = client.get("/prospects", auth=("admin", "pw"))
    assert page.status_code == 200 and "Heistguy" in page.text
    assert "https://media.example.test/p/join?ref=" in page.text, "invites use the public URL"
    env.public_base_url = ""
    assert "PUBLIC_BASE_URL" in client.get("/prospects", auth=("admin", "pw")).text, "warns when links are local-only"
    pid = db.q1("SELECT id FROM prospects")["id"]
    assert client.post(f"/prospects/{pid}/invited", auth=("admin", "pw"), data={"csrf": client.csrf},
                       follow_redirects=False).status_code == 303
    assert db.q1("SELECT status FROM prospects")["status"] == "invited"
    h = {"Authorization": f"Bearer {security.api_token()}"}
    [p] = client.get("/api/v1/prospects?status=invited", headers=h).json()
    assert p["handle"] == "@heistguy" and p["invite_text"]
    assert client.post(f"/api/v1/prospects/{pid}/ignored", headers=h).json()["status"] == "ignored"
    assert client.post(f"/api/v1/prospects/{pid}/joined", headers=h).status_code == 422, "joined comes from sign-up only"


def test_discover_stage_runs_without_any_source(client, env, monkeypatch):
    h = {"Authorization": f"Bearer {security.api_token()}"}
    r = client.post("/api/v1/run/discover?wait=20", headers=h).json()
    assert r["status"] == "done" and r["result"]["found"] == 0
    assert any("YouTube not connected" in n for n in r["result"]["notes"])
    assert not autopilot._locks["discover"].locked()
