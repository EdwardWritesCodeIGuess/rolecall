"""Publisher interface. Each platform keeps a resumable checkpoint in posts.state so a retry
continues the same upload instead of creating a duplicate post."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


class PublishError(Exception):
    """retryable=False means stop retrying (bad auth, policy rejection, invalid media)."""

    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


class NotConnected(PublishError):
    def __init__(self, platform: str, why: str = "not connected"):
        super().__init__(f"{platform}: {why}", retryable=False)


@dataclass
class PostJob:
    post_id: int
    clip_id: int
    platform: str
    video_path: str
    thumb_path: str | None
    copy: dict
    idempotency_key: str
    state: dict = field(default_factory=dict)

    def checkpoint(self, **kv) -> None:
        """Persist progress immediately; survives crashes and restarts."""
        from .. import db

        self.state.update(kv)
        db.ex("UPDATE posts SET state=? WHERE id=?", (json.dumps(self.state), self.post_id))


@dataclass
class PublishResult:
    remote_id: str
    remote_url: str | None
    status: str = "published"   # published|processing|export_ready


class Publisher:
    platform = "base"

    def connected(self) -> tuple[bool, str]:
        raise NotImplementedError

    def publish(self, job: PostJob) -> PublishResult:
        raise NotImplementedError

    def fetch_metrics(self, remote_id: str) -> dict | None:
        return None

    def account_followers(self) -> int | None:
        return None
