"""Verdicts for fetched pages (no I/O): is a link alive, dead or unknown.

Only unambiguous signals count as "dead": HTTP 404/410, a domain that does not exist, a t.me page without
a title (the username / invite does not exist) or a t.me post widget with an error. Everything else that is
not clearly alive (403, 429, 5xx, timeouts, TLS problems, unexpected markup) is "unknown".
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass

ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"
SKIP = "skip"  # links we cannot check (t.me/c/..., service pages of Telegram)

_TITLE_RE = re.compile(r'<div class="tgme_page_title(?:\s[^"]*)?"[^>]*>(.*?)</div>', re.S)
_DESCRIPTION_RE = re.compile(r'<div class="tgme_page_description(?:\s[^"]*)?"[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Page:
    """Result of one HTTP request. ``status`` is None when no response was received."""

    status: int | None
    text: str = ""
    error: str | None = None  # dns / timeout / tls / redirects / network
    final_url: str | None = None


@dataclass(frozen=True)
class Verdict:
    state: str
    detail: str = ""
    title: str | None = None  # human readable page / chat title
    fingerprint: str | None = None  # normalized title, to notice a username takeover

    @property
    def usable(self) -> str:
        return UNKNOWN if self.state == SKIP else self.state


def fingerprint(title: str | None) -> str | None:
    """Letters and digits only, case-folded: survives emoji / punctuation / spacing changes."""
    if not title:
        return None
    normalized = unicodedata.normalize("NFKC", title).casefold()
    result = "".join(char for char in normalized if char.isalnum())
    return result[:256] or None


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", fragment)).split())


def tme_title(page_html: str) -> str | None:
    match = _TITLE_RE.search(page_html)
    return _text(match.group(1)) if match else None


def tme_description(page_html: str) -> str:
    match = _DESCRIPTION_RE.search(page_html)
    return _text(match.group(1)) if match else ""


def tme_verdict(page: Page) -> Verdict:
    """t.me/<username> or t.me/+<invite>: the page has a title only when the chat / user exists."""
    if page.status is None:
        return Verdict(UNKNOWN, page.error or "network")
    if page.status in (404, 410):
        return Verdict(DEAD, f"HTTP {page.status}")
    if page.status != 200:
        return Verdict(UNKNOWN, f"HTTP {page.status}")
    if "tgme_page" not in page.text:
        return Verdict(UNKNOWN, "unexpected t.me page")
    title = tme_title(page.text)
    if not title:
        return Verdict(DEAD, "t.me: no such chat")
    return Verdict(ALIVE, "t.me", title=title, fingerprint=fingerprint(title))


def post_verdict(page: Page) -> Verdict:
    """t.me/<channel>/<id>?embed=1: the widget shows an error block for a missing post."""
    if page.status is None:
        return Verdict(UNKNOWN, page.error or "network")
    if page.status in (404, 410):
        return Verdict(DEAD, f"HTTP {page.status}")
    if page.status != 200:
        return Verdict(UNKNOWN, f"HTTP {page.status}")
    if "tgme_widget_message_error" in page.text:
        return Verdict(DEAD, "t.me: post not found")
    if "tgme_widget_message" in page.text:
        return Verdict(ALIVE, "t.me post")
    return Verdict(UNKNOWN, "unexpected widget page")


def http_verdict(page: Page) -> Verdict:
    """External site after following redirects."""
    if page.error == "dns":
        return Verdict(DEAD, "domain does not exist")
    if page.status is None:
        return Verdict(UNKNOWN, page.error or "network")
    if page.status in (404, 410):
        return Verdict(DEAD, f"HTTP {page.status}")
    if 200 <= page.status < 400:
        return Verdict(ALIVE, f"HTTP {page.status}")
    return Verdict(UNKNOWN, f"HTTP {page.status}")
