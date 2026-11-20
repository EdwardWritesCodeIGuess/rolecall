"""Encrypted token storage and short-lived signed media URLs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import get_config


def _secret() -> bytes:
    cfg = get_config()
    if cfg.secret_key:
        return cfg.secret_key.encode()
    # First run without SECRET_KEY: generate one and keep it next to the database.
    p = cfg.data_dir / ".secret_key"
    if not p.exists():
        p.write_text(secrets.token_urlsafe(48))
        p.chmod(0o600)
    return p.read_text().strip().encode()


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(b"rolecall-tokens:" + _secret()).digest())
    return Fernet(key)


def save_token(platform: str, data: dict, account_name: str | None = None) -> None:
    blob = _fernet().encrypt(json.dumps(data).encode())
    db.ex("INSERT INTO tokens(platform, data, account_name, updated_at) VALUES(?,?,?,?) "
          "ON CONFLICT(platform) DO UPDATE SET data=excluded.data, "
          "account_name=COALESCE(excluded.account_name, tokens.account_name), updated_at=excluded.updated_at",
          (platform, blob, account_name, db.now_iso()))


def load_token(platform: str) -> dict | None:
    row = db.q1("SELECT data FROM tokens WHERE platform=?", (platform,))
    if not row:
        return None
    try:
        return json.loads(_fernet().decrypt(row["data"]))
    except InvalidToken:
        db.log_event("error", "token", f"Could not decrypt {platform} token: SECRET_KEY changed? Reconnect it.")
        return None


def delete_token(platform: str) -> None:
    db.ex("DELETE FROM tokens WHERE platform=?", (platform,))


def sign(path: str, ttl: int = 3600) -> str:
    exp = int(time.time()) + ttl
    mac = hmac.new(_secret(), f"{path}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}-{mac}"


def verify(path: str, token: str) -> bool:
    try:
        exp_s, mac = token.split("-", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    good = hmac.new(_secret(), f"{path}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(good, mac)


def random_token(n: int = 18) -> str:
    return secrets.token_urlsafe(n)
