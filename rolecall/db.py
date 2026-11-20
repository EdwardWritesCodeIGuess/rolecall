"""SQLite storage. One file, WAL mode, safe for the web process + worker thread."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import get_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS creators (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  primary_url TEXT,
  contact_email TEXT,
  credit_text TEXT,
  layout TEXT NOT NULL DEFAULT 'auto',      -- auto|blur|split|crop
  facecam_box TEXT,                          -- JSON {x,y,w,h} as 0..1 fractions of the source frame
  auto_approve INTEGER NOT NULL DEFAULT 0,   -- 1 = clips with no flags publish without review
  is_client INTEGER NOT NULL DEFAULT 0,      -- paying clipping customer: gets unbranded deliveries
  delivery_token TEXT,
  watch_url TEXT,                            -- optional channel/VOD list to auto-ingest (authorized only)
  verify_code TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS permissions (
  id INTEGER PRIMARY KEY,
  creator_id INTEGER NOT NULL REFERENCES creators(id),
  status TEXT NOT NULL DEFAULT 'pending',    -- pending|active|revoked|expired
  verified INTEGER NOT NULL DEFAULT 0,       -- grantor identity confirmed as the channel owner
  source_channels TEXT NOT NULL DEFAULT '[]',-- JSON list of channel URLs the grant covers
  platforms TEXT NOT NULL DEFAULT '[]',      -- JSON list: tiktok|instagram|youtube
  allow_edit INTEGER NOT NULL DEFAULT 0,
  allow_repost INTEGER NOT NULL DEFAULT 0,
  allow_monetize INTEGER NOT NULL DEFAULT 0,
  credit_required INTEGER NOT NULL DEFAULT 1,
  credit_format TEXT,
  revenue_share_pct REAL NOT NULL DEFAULT 0,
  revenue_terms TEXT,
  granted_by TEXT,
  evidence TEXT,                             -- form submission id, email thread, signed doc path
  granted_at TEXT,
  expires_at TEXT,
  revoked_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  creator_id INTEGER NOT NULL REFERENCES creators(id),
  permission_id INTEGER REFERENCES permissions(id),
  kind TEXT NOT NULL,                        -- upload|url|inbox|submission|demo
  origin TEXT,
  title TEXT,
  path TEXT,
  audio_only INTEGER NOT NULL DEFAULT 0,
  duration REAL, width INTEGER, height INTEGER, fps REAL,
  sha256 TEXT UNIQUE,
  is_demo INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new',        -- new|processing|done|error
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transcripts (
  source_id INTEGER PRIMARY KEY REFERENCES sources(id),
  model TEXT, language TEXT, data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS moments (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  start REAL NOT NULL, end REAL NOT NULL,
  fmt TEXT NOT NULL DEFAULT 'punch',         -- punch|story|long
  score REAL, hook REAL, context REAL, payoff REAL,
  reasons TEXT, text TEXT,
  status TEXT NOT NULL DEFAULT 'candidate'   -- candidate|selected|rendered|rejected
);

CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY,
  moment_id INTEGER REFERENCES moments(id),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  creator_id INTEGER NOT NULL REFERENCES creators(id),
  fmt TEXT, layout TEXT,
  path TEXT, thumb_path TEXT, delivery_path TEXT,
  duration REAL,
  title TEXT,
  copy TEXT,                                 -- JSON per-platform titles/captions
  flags TEXT NOT NULL DEFAULT '[]',          -- JSON list of {code, severity, detail}
  review_status TEXT NOT NULL DEFAULT 'pending', -- pending|needs_review|approved|auto_approved|rejected
  reviewed_at TEXT,
  render_seconds REAL,
  cost_cents REAL NOT NULL DEFAULT 0,
  score REAL,
  is_demo INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  platform TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'api',          -- api|export
  status TEXT NOT NULL DEFAULT 'queued',     -- queued|uploading|processing|published|export_ready|failed|cancelled
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL DEFAULT '{}',          -- resumable checkpoint (upload URI, container id, ...)
  remote_id TEXT, remote_url TEXT,
  last_error TEXT,
  published_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(clip_id, platform)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  level TEXT NOT NULL, kind TEXT NOT NULL,
  ref_type TEXT, ref_id INTEGER, message TEXT
);

CREATE TABLE IF NOT EXISTS post_metrics (
  id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL REFERENCES posts(id),
  captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  views INTEGER, watch_time_sec REAL, avg_view_pct REAL,
  likes INTEGER, comments INTEGER, shares INTEGER, followers_gained INTEGER
);

CREATE TABLE IF NOT EXISTS account_stats (
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL,
  captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, followers INTEGER
);

CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL, kind TEXT NOT NULL,    -- revenue|cost
  category TEXT NOT NULL,                    -- platform_payout|client|compute|llm|software|other
  platform TEXT, creator_id INTEGER, clip_id INTEGER,
  amount_cents INTEGER NOT NULL, note TEXT
);

CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  name TEXT, email TEXT, channel_url TEXT, avg_viewers TEXT, package TEXT, message TEXT,
  status TEXT NOT NULL DEFAULT 'new'         -- new|contacted|trial|client|lost
);

CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  creator_id INTEGER REFERENCES creators(id),
  permission_id INTEGER REFERENCES permissions(id),
  name TEXT, email TEXT, channel_url TEXT, footage_url TEXT, file_path TEXT,
  consent TEXT, ip TEXT, status TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS removal_requests (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  name TEXT, email TEXT, channel_url TEXT, creator_id INTEGER, urls TEXT, reason TEXT,
  status TEXT NOT NULL DEFAULT 'open',       -- open|done|rejected
  actions TEXT
);

CREATE TABLE IF NOT EXISTS tokens (
  platform TEXT PRIMARY KEY,
  data BLOB NOT NULL,                        -- Fernet-encrypted JSON
  account_name TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS watch_seen (       -- VODs the stream watcher has looked at
  url TEXT PRIMARY KEY,
  creator_id INTEGER REFERENCES creators(id),
  status TEXT NOT NULL,                      -- ingested|skipped|live|error
  reason TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS prospects (        -- streamers found by discovery, to invite (never clipped as-is)
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,                    -- youtube|twitch
  channel_id TEXT NOT NULL,
  handle TEXT, name TEXT, url TEXT,
  last_title TEXT, last_stream_url TEXT, language TEXT,
  peak_viewers INTEGER, followers INTEGER,
  seen_count INTEGER NOT NULL DEFAULT 0,
  seen_days TEXT NOT NULL DEFAULT '[]',      -- JSON list of UTC dates seen streaming the game
  first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen TEXT,
  score REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new',        -- new|invited|joined|ignored|declined
  invite_token TEXT UNIQUE,                  -- /p/join?ref=<token>
  creator_id INTEGER REFERENCES creators(id),
  notified INTEGER NOT NULL DEFAULT 0, invited_at TEXT, notes TEXT,
  UNIQUE(platform, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, platform);
CREATE INDEX IF NOT EXISTS idx_clips_review ON clips(review_status);
CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "paused": False,                       # global kill switch for publishing
    "paused_youtube": False,
    "paused_instagram": False,
    "paused_tiktok": False,
    "daily_limit_youtube": 3,
    "daily_limit_instagram": 3,
    "daily_limit_tiktok": 3,
    "min_gap_minutes": 120,                # spacing between posts on one platform
    "post_window_start": 10,               # local hour; posts only go out inside this window
    "post_window_end": 23,
    "platforms_enabled": ["youtube", "instagram"],  # add "tiktok" once a TikTok account exists
    "tiktok_mode": "export",               # export|inbox (inbox needs approved video.upload scope)
    "instagram_mode": "export",            # api|export (export = post by hand at instagram.com; user's choice)
    "clips_per_source": 4,
    "min_moment_score": 0.6,
    "formats": ["punch", "story"],         # add "long" (61-90s) for TikTok Creator Rewards eligibility
    "trusted_after": 3,                    # human-approved clips before a creator is auto-trusted
    "max_attempts": 5,
    # Automation (n8n). auto = n8n drives watch/process/publish while it keeps calling /api/v1/run/*;
    # the built-in worker takes over if n8n is quiet for n8n_grace_minutes. n8n|builtin force one.
    "orchestrator": "auto",
    "n8n_grace_minutes": 90,
    # Stream watcher (creators with a watch_url and an active, verified permission)
    "watch_every_minutes": 60,             # built-in worker cadence; n8n uses its own schedule
    "watch_lookback": 5,                   # newest N VODs checked per channel
    "watch_max_age_days": 14,              # ignore older VODs
    "watch_per_run": 1,                    # new VODs ingested per channel per run (keeps CPU/disk bounded)
    "ocr_enabled": True,                   # scan rendered frames for on-screen personal info (needs tesseract)
    # Discovery: find GTA 6 streamers to invite (they opt in at /p/join before anything is clipped)
    "discover_enabled": True,
    "discover_every_minutes": 180,         # built-in worker cadence; n8n workflow 04 uses its own schedule
    "discover_youtube_query": "GTA 6|GTA VI|Grand Theft Auto VI",
    "discover_twitch_games": ["Grand Theft Auto VI"],
    "discover_languages": ["en"],
    "discover_min_score": 0.5,             # alert only about prospects at or above this fit score
    "discover_notify_top": 5,
    "invite_template": ("Hey {name}! Caught your GTA 6 stream (\"{title}\"). I run {brand} (@{handle}): we turn "
                        "streams into Shorts and Reels with your name credited on every clip, free for you. "
                        "Want in? Takes a minute: {link} . You can pull any clip anytime."),
}

_local = threading.local()


def _connect() -> sqlite3.Connection:
    cfg = get_config()
    conn = sqlite3.connect(cfg.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    path = str(get_config().db_path)
    if c is None or getattr(_local, "path", None) != path:
        c = _connect()
        _local.conn = c
        _local.path = path
    return c


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def init_db() -> None:
    c = conn()
    c.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, json.dumps(v)))
    c.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def q(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def q1(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    return conn().execute(sql, params).fetchone()


def ex(sql: str, params: tuple | dict = ()) -> int:
    c = conn()
    cur = c.execute(sql, params)
    c.commit()
    return cur.lastrowid


def get_setting(key: str) -> Any:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return json.loads(row["value"])


def set_setting(key: str, value: Any) -> None:
    ex("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, json.dumps(value)))


def log_event(level: str, kind: str, message: str, ref_type: str | None = None, ref_id: int | None = None) -> None:
    ex("INSERT INTO events(level, kind, ref_type, ref_id, message) VALUES(?,?,?,?,?)",
       (level, kind, ref_type, ref_id, message[:4000]))
    if level in {"error", "action"}:
        from .notify import notify  # local import to avoid a cycle

        notify(f"[{level}] {kind}: {message[:500]}")


def jload(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default
