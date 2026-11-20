"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("ROLECALL_DATA", "./data")).resolve())
    brand_name: str = os.environ.get("BRAND_NAME", "Rolecall")
    brand_handle: str = os.environ.get("BRAND_HANDLE", "rolecallclips")
    # Base URL the dashboard is reached at (used for OAuth redirects).
    base_url: str = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
    # Public HTTPS URL that Instagram/TikTok servers can fetch media from (Tailscale Funnel,
    # Cloudflare Tunnel, ...). Empty = Instagram publishing is disabled with a clear error.
    public_base_url: str = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    admin_user: str = os.environ.get("ADMIN_USER", "admin")
    admin_password: str = os.environ.get("ADMIN_PASSWORD", "")
    secret_key: str = os.environ.get("SECRET_KEY", "")
    timezone: str = os.environ.get("TZ", "America/New_York")
    whisper_model: str = os.environ.get("WHISPER_MODEL", "base.en")
    whisper_device: str = os.environ.get("WHISPER_DEVICE", "cpu")
    worker_enabled: bool = _bool("WORKER_ENABLED", True)
    worker_interval: int = int(os.environ.get("WORKER_INTERVAL", "20"))
    caption_font: str = os.environ.get("CAPTION_FONT", "")
    # Optional paid services: empty = disabled.
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
    notify_webhook: str = os.environ.get("NOTIFY_WEBHOOK", "")  # Discord/ntfy/Slack-compatible
    # n8n automation: bearer token for /api/v1 (empty = generated into data/.api_token) and the
    # n8n webhook Rolecall sends events to (clip ready, needs review, posted, failed).
    api_token: str = os.environ.get("ROLECALL_API_TOKEN", "")
    n8n_webhook_url: str = os.environ.get("N8N_WEBHOOK_URL", "")
    # Platform app credentials (created by the account owner; see docs/LAUNCH.md).
    google_client_id: str = os.environ.get("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    instagram_app_id: str = os.environ.get("INSTAGRAM_APP_ID", "")
    instagram_app_secret: str = os.environ.get("INSTAGRAM_APP_SECRET", "")
    tiktok_client_key: str = os.environ.get("TIKTOK_CLIENT_KEY", "")
    tiktok_client_secret: str = os.environ.get("TIKTOK_CLIENT_SECRET", "")
    # Twitch app (dev.twitch.tv, free) for GTA 6 stream discovery. Empty = YouTube discovery only.
    twitch_client_id: str = os.environ.get("TWITCH_CLIENT_ID", "")
    twitch_client_secret: str = os.environ.get("TWITCH_CLIENT_SECRET", "")
    # Compute cost estimate for cost-per-clip: homelab electricity + wear, USD per CPU-hour.
    compute_cost_per_hour: float = float(os.environ.get("COMPUTE_COST_PER_HOUR", "0.05"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rolecall.db"

    def dir(self, *parts: str) -> Path:
        p = self.data_dir.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
        _config.data_dir.mkdir(parents=True, exist_ok=True)
    return _config


def reset_config(**overrides) -> Config:
    """Used by tests to point at a temp data dir."""
    global _config
    _config = Config(**overrides)
    _config.data_dir.mkdir(parents=True, exist_ok=True)
    return _config
