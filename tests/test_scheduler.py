"""Publishing rules: pause switch, daily caps, spacing, retries, demo + permission guards."""

import json
from datetime import datetime, timezone

from rolecall import db, pipeline, scheduler
from rolecall.publishers import PublishError, PublishResult

from .conftest import make_clip, make_creator


class FakePub:
    platform = "youtube"

    def __init__(self, fail=None):
        self.n = 0
        self.fail = fail

    def connected(self):
        return True, "ok"

    def publish(self, job):
        self.n += 1
        if self.fail:
            raise self.fail
        return PublishResult(f"V{job.post_id}", f"https://youtube.com/shorts/V{job.post_id}")


def setup_posts(tmp_path, n=3):
    cid = make_creator()
    ids = []
    for i in range(n):
        clip = make_clip(cid, tmp_path / f"c{i}.mp4", score=0.9 - i * 0.1)
        ids += pipeline.queue_posts(clip)
    return cid, ids


def test_queue_respects_permission_platforms(env, tmp_path):
    cid = make_creator(platforms=("youtube",))
    clip = make_clip(cid, tmp_path / "x.mp4")
    pipeline.queue_posts(clip)
    assert [r["platform"] for r in db.q("SELECT platform FROM posts")] == ["youtube"]
    pipeline.queue_posts(clip)  # idempotent
    assert db.q1("SELECT COUNT(*) n FROM posts")["n"] == 1


def test_pause_blocks_everything(env, tmp_path, monkeypatch):
    setup_posts(tmp_path)
    fake = FakePub()
    monkeypatch.setattr(scheduler, "get_publisher", lambda p: fake)
    db.set_setting("paused", True)
    scheduler.publish_due()
    assert fake.n == 0
    db.set_setting("paused", False)
    db.set_setting("paused_youtube", True)
    db.set_setting("platforms_enabled", ["youtube"])
    scheduler.publish_due()
    assert fake.n == 0


def test_daily_limit_and_spacing(env, tmp_path, monkeypatch):
    setup_posts(tmp_path)
    fake = FakePub()
    monkeypatch.setattr(scheduler, "get_publisher", lambda p: fake)
    db.set_setting("platforms_enabled", ["youtube"])
    db.set_setting("min_gap_minutes", 0)
    db.set_setting("daily_limit_youtube", 2)
    for _ in range(5):
        scheduler.publish_due()
    assert fake.n == 2, "daily limit caps posts"
    first = db.q("SELECT * FROM posts WHERE status='published' ORDER BY id")
    assert len(first) == 2
    assert db.q1("SELECT c.score FROM posts p JOIN clips c ON c.id=p.clip_id WHERE p.status='published' "
                 "ORDER BY p.published_at LIMIT 1")["score"] == 0.9, "highest score posts first"
    db.set_setting("daily_limit_youtube", 10)
    db.set_setting("min_gap_minutes", 120)
    scheduler.publish_due()
    assert fake.n == 2, "spacing prevents back-to-back posts"


def test_posting_window(env):
    db.set_setting("post_window_start", 10)
    db.set_setting("post_window_end", 12)
    ok, why = scheduler.can_post_now("youtube", datetime(2026, 1, 1, 3, tzinfo=timezone.utc))
    assert not ok and "window" in why
    ok, _ = scheduler.can_post_now("youtube", datetime(2026, 1, 1, 11, tzinfo=timezone.utc))
    assert ok


def test_retry_backoff_then_fail(env, tmp_path, monkeypatch):
    setup_posts(tmp_path, n=1)
    fake = FakePub(fail=PublishError("503 backend error", retryable=True))
    monkeypatch.setattr(scheduler, "get_publisher", lambda p: fake)
    db.set_setting("platforms_enabled", ["youtube"])
    scheduler.publish_due()
    p = db.q1("SELECT * FROM posts WHERE platform='youtube'")
    assert p["status"] == "queued" and p["attempts"] == 1 and p["next_attempt_at"]
    scheduler.publish_due()
    assert fake.n == 1, "backoff: not retried before next_attempt_at"
    db.set_setting("max_attempts", 2)
    db.ex("UPDATE posts SET next_attempt_at=NULL")
    scheduler.publish_due()
    assert db.q1("SELECT status FROM posts WHERE platform='youtube'")["status"] == "failed"


def test_non_retryable_fails_immediately(env, tmp_path, monkeypatch):
    setup_posts(tmp_path, n=1)
    monkeypatch.setattr(scheduler, "get_publisher", lambda p: FakePub(fail=PublishError("bad token", retryable=False)))
    db.set_setting("platforms_enabled", ["youtube"])
    scheduler.publish_due()
    assert db.q1("SELECT status FROM posts WHERE platform='youtube'")["status"] == "failed"


def test_demo_never_publishes(env, tmp_path, monkeypatch):
    cid = make_creator()
    clip = make_clip(cid, tmp_path / "demo.mp4", is_demo=1)
    db.ex("INSERT INTO posts(clip_id, platform, idempotency_key) VALUES(?,?,?)", (clip, "youtube", "demo-key"))
    fake = FakePub()
    monkeypatch.setattr(scheduler, "get_publisher", lambda p: fake)
    db.set_setting("platforms_enabled", ["youtube"])
    scheduler.publish_due()
    assert fake.n == 0
    assert db.q1("SELECT status FROM posts")["status"] == "cancelled"


def test_revoked_permission_cancels_queued_post(env, tmp_path, monkeypatch):
    cid, _ = setup_posts(tmp_path, n=1)
    db.ex("UPDATE permissions SET status='revoked' WHERE creator_id=?", (cid,))
    fake = FakePub()
    monkeypatch.setattr(scheduler, "get_publisher", lambda p: fake)
    db.set_setting("platforms_enabled", ["youtube"])
    scheduler.publish_due()
    assert fake.n == 0
    assert db.q1("SELECT status FROM posts WHERE platform='youtube'")["status"] == "cancelled"


def test_removal_request_cancels_pending(env, tmp_path):
    cid, _ = setup_posts(tmp_path, n=2)
    rid = db.ex("INSERT INTO removal_requests(name, email, creator_id) VALUES('x','x@example.test',?)", (cid,))
    pipeline.handle_removal(rid)
    assert db.q1("SELECT COUNT(*) n FROM posts WHERE status!='cancelled'")["n"] == 0
    assert json.loads(db.q1("SELECT actions FROM removal_requests")["actions"])
