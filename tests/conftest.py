import json
from pathlib import Path

import pytest

from rolecall import db
from rolecall.config import reset_config


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    cfg = reset_config(data_dir=tmp_path, secret_key="test-secret", admin_password="pw", timezone="UTC",
                       public_base_url="https://media.example.test", google_client_id="cid",
                       google_client_secret="csec")
    db.init_db()
    db.set_setting("post_window_start", 0)
    db.set_setting("post_window_end", 24)
    yield cfg


def make_creator(slug="streamer", verified=True, monetize=True, platforms=("youtube", "instagram", "tiktok"),
                 channels=("https://www.twitch.tv/streamer",)) -> int:
    cid = db.ex("INSERT INTO creators(slug, display_name, primary_url, credit_text) VALUES(?,?,?,?)",
                (slug, slug.title(), channels[0], f"twitch.tv/{slug}"))
    db.ex("INSERT INTO permissions(creator_id, status, verified, source_channels, platforms, allow_edit, allow_repost,"
          " allow_monetize, granted_by, evidence) VALUES(?,?,?,?,?,1,1,?,?,?)",
          (cid, "active" if verified else "pending", int(verified), json.dumps(list(channels)), json.dumps(list(platforms)),
           int(monetize), "Test Streamer", "unit test"))
    return cid


def make_clip(creator_id: int, path: Path, review_status="approved", is_demo=0, score=0.8) -> int:
    path.write_bytes(b"\x00" * 2048)
    sid = db.ex("INSERT INTO sources(creator_id, kind, path, duration, sha256, is_demo) VALUES(?,?,?,?,?,?)",
                (creator_id, "upload", str(path), 30, f"sha{path.name}", is_demo))
    copy = {"title": "Test title", "youtube": {"title": "Test title", "description": "d", "tags": ["gta rp"]},
            "instagram": {"caption": f"Caption for {path.name}"}, "tiktok": {"caption": "tt caption"}}
    return db.ex("INSERT INTO clips(source_id, creator_id, path, duration, title, copy, review_status, is_demo, score)"
                 " VALUES(?,?,?,?,?,?,?,?,?)", (sid, creator_id, str(path), 20, "Test title", json.dumps(copy),
                                                review_status, is_demo, score))
