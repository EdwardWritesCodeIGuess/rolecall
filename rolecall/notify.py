"""Optional push alerts (Discord / Slack / ntfy-compatible webhook). Free; disabled when unset."""

from __future__ import annotations

import threading

import httpx

from .config import get_config


def _send(url: str, text: str) -> None:
    try:
        if "discord.com/api/webhooks" in url:
            httpx.post(url, json={"content": text[:1900]}, timeout=10)
        elif "hooks.slack.com" in url:
            httpx.post(url, json={"text": text}, timeout=10)
        else:  # ntfy.sh and generic endpoints accept a plain body
            httpx.post(url, content=text.encode(), timeout=10)
    except httpx.HTTPError:
        pass


def notify(text: str) -> None:
    url = get_config().notify_webhook
    if url:
        threading.Thread(target=_send, args=(url, text), daemon=True).start()
