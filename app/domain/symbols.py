"""Symbolic links inside stored fragments, resolved at render time.

Stored posts never contain concrete links to our own posts or bot; they contain symbols like ``post:nav``,
``post:cat:12`` or ``bot:start:add_{slug}``. When the channel or bot changes, re-rendering re-targets them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def channel_post_base(chat_id: int, username: str | None) -> str:
    if username:
        return f"https://t.me/{username}/"
    raw = str(chat_id)
    internal = raw[4:] if raw.startswith("-100") else raw.lstrip("-")
    return f"https://t.me/c/{internal}/"


def channel_url(chat_id: int, username: str | None, invite_link: str | None) -> str | None:
    if username:
        return f"https://t.me/{username}"
    return invite_link


@dataclass
class LinkContext:
    bot_username: str | None = None
    post_base: str | None = None  # base URL for posts of the channel being rendered
    posts: dict[str, int] = field(default_factory=dict)  # "nav" / "cat:5" / "static:2" / "scam:3" -> msg id
    channels: dict[str, str] = field(default_factory=dict)  # "main" / "scam" -> url
    scam_post_base: str | None = None
    scam_cards: dict[int, int] = field(default_factory=dict)  # scam entry id -> message id

    def post_url(self, key: str) -> str | None:
        if self.post_base is None:
            return None
        message_id = self.posts.get(key)
        if not message_id:
            return None
        return f"{self.post_base}{message_id}"

    def resolve(self, url: str, *, service_url: str | None = None, slug: str | None = None) -> str | None:
        if "://" in url:
            return url
        if url.startswith("post:"):
            return self.post_url(url[len("post:") :])
        if url == "bot":
            return f"https://t.me/{self.bot_username}" if self.bot_username else None
        if url.startswith("bot:start:"):
            if not self.bot_username:
                return None
            payload = url[len("bot:start:") :]
            if slug is not None:
                payload = payload.replace("{slug}", slug)
            return f"https://t.me/{self.bot_username}?start={payload}"
        if url.startswith("channel:"):
            return self.channels.get(url[len("channel:") :])
        if url == "service:url":
            return service_url
        if url.startswith("scam:card:"):
            try:
                entry_id = int(url[len("scam:card:") :])
            except ValueError:
                return None
            message_id = self.scam_cards.get(entry_id)
            if not message_id or not self.scam_post_base:
                return None
            return f"{self.scam_post_base}{message_id}"
        return url
