from .base import NotConnected, PostJob, PublishError, PublishResult, Publisher
from .instagram import Instagram
from .tiktok import TikTok
from .youtube import YouTube

PLATFORMS = ["youtube", "instagram", "tiktok"]


def get_publisher(platform: str) -> Publisher:
    return {"youtube": YouTube, "instagram": Instagram, "tiktok": TikTok}[platform]()


__all__ = ["PLATFORMS", "get_publisher", "PostJob", "PublishError", "PublishResult", "NotConnected", "Publisher"]
