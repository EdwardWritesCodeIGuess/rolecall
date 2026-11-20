"""Full pipeline on the labeled demo footage: generate -> ingest -> transcribe -> rank -> render -> review.

Then the same rendered file goes through approve -> queue -> publish against a mocked YouTube API,
so the publish path is exercised end-to-end without touching a real account.
Run with: pytest -m slow
"""

import json
import shutil
import time

import httpx
import pytest

from rolecall import db, demo, media, pipeline, scheduler, security
from rolecall.publishers.youtube import YouTube

from .test_publishers import YTFake

pytestmark = pytest.mark.slow


@pytest.mark.skipif(not (shutil.which("say") or shutil.which("espeak-ng")), reason="needs a TTS engine")
def test_demo_end_to_end(env, monkeypatch):
    cid = demo.ensure_demo_creator()
    src_file = demo.make_demo(env.data_dir / "demo.mp4")
    sid = pipeline.ingest_file(cid, src_file, kind="demo", is_demo=True, move=True)
    clip_ids = pipeline.process_source(sid)
    assert len(clip_ids) >= 2
    for c in db.q("SELECT * FROM clips"):
        info = media.probe(c["path"])
        assert (info["width"], info["height"]) == (1080, 1920)
        assert info["has_audio"] and 8 <= info["duration"] <= 61
        flags = json.loads(c["flags"])
        assert flags[0]["code"] == "demo_footage", "demo clips are always blocked"
        assert c["review_status"] == "needs_review"
    titles = [c["title"] for c in db.q("SELECT title FROM clips")]
    assert len(set(titles)) == len(titles), "titles are unique per source"
    with pytest.raises(pipeline.PermissionDenied):
        pipeline.queue_posts(clip_ids[0])

    # Publish path with mocked YouTube: a real authorized creator, reusing the rendered file as their clip.
    real = db.ex("INSERT INTO creators(slug, display_name, primary_url) VALUES('authorized','Authorized',"
                 "'https://twitch.tv/authorized')")
    db.ex("INSERT INTO permissions(creator_id, status, verified, source_channels, platforms, allow_edit, allow_repost,"
          " allow_monetize) VALUES(?, 'active', 1, '[\"https://twitch.tv/authorized\"]', '[\"youtube\"]', 1, 1, 1)", (real,))
    rendered = db.q1("SELECT * FROM clips WHERE id=?", (clip_ids[0],))
    clip = db.ex("INSERT INTO clips(source_id, creator_id, path, duration, title, copy, flags, review_status, score)"
                 " VALUES(?,?,?,?,?,?,?,?,?)", (rendered["source_id"], real, rendered["path"], rendered["duration"],
                                                rendered["title"], rendered["copy"], "[]", "needs_review", 0.9))
    pipeline.approve(clip)
    security.save_token("youtube", {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3000})
    fake = YTFake()
    monkeypatch.setattr(scheduler, "get_publisher",
                        lambda p: YouTube(httpx.Client(transport=httpx.MockTransport(fake))))
    db.set_setting("platforms_enabled", ["youtube"])
    scheduler.publish_due()
    post = db.q1("SELECT * FROM posts WHERE clip_id=?", (clip,))
    assert post["status"] == "published" and post["remote_url"] == "https://www.youtube.com/shorts/VID123"
    scheduler.publish_due()
    assert sum(1 for m, _ in fake.calls if m == "POST") == 1, "no duplicate upload on later ticks"
