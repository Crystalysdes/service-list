"""Normalization and classification of service links (t.me and external https)."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TG_HOSTS = frozenset(
    {"t.me", "telegram.me", "telegram.dog", "www.t.me", "www.telegram.me", "www.telegram.dog"}
)
# names that never lead to a public site: the checker must not be sent to the bot's own network
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
INVITE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
# zero-width, bidi controls, BOM
FORBIDDEN_CHARS = frozenset("​‌‍⁠﻿‎‏‪‫‬‭‮⁦⁧⁨⁩")
RESERVED_TG_PATHS = frozenset(
    {
        "addstickers",
        "addemoji",
        "addlist",
        "addtheme",
        "proxy",
        "socks",
        "share",
        "iv",
        "setlanguage",
        "login",
        "confirmphone",
        "bg",
        "invoice",
        "m",
        "boost",
        "giftcode",
        "nft",
        "contact",
        "addstory",
    }
)
BOT_QUERY_KEYS = ("start", "startgroup", "startapp", "startchannel", "admin")
_BARE_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)")


class LinkError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Link:
    url: str
    kind: str  # tg_username / tg_invite / tg_post / tg_private / tg_other / external
    username: str | None = None  # lowercased
    invite: str | None = None
    post_id: int | None = None
    host: str | None = None

    @property
    def is_telegram(self) -> bool:
        return self.kind.startswith("tg_")


def has_forbidden_chars(value: str) -> bool:
    for char in value:
        if char in FORBIDDEN_CHARS:
            return True
        if unicodedata.category(char) in ("Cc", "Cs", "Co"):
            return True
    return False


# characters that print nothing although they are letters or symbols (a "blank" name, a fake empty line)
BLANK_CHARS = frozenset("\u115f\u1160\u3164\uffa0\u2800\u180e")


def clean_text(value: str, *, allow_newlines: bool = False) -> str:
    """Validate free text coming from users (names, descriptions): no control or invisible characters, no
    line breaks hidden as separators (a name cannot pose as two lines, e.g. a fake "✅ verified" one)."""
    value = value.strip()
    for char in value:
        if char in FORBIDDEN_CHARS or char in BLANK_CHARS:
            raise LinkError("bad_chars")
        category = unicodedata.category(char)
        if category in ("Cf", "Zl", "Zp"):  # soft hyphens, tags, marks, U+2028/U+2029...
            raise LinkError("bad_chars")
        if category in ("Cc", "Cs", "Co") and not (allow_newlines and char == "\n"):
            raise LinkError("bad_chars")
    return value


def _tg_username_link(username: str, query: dict[str, str] | None = None) -> Link:
    if not USERNAME_RE.match(username):
        raise LinkError("tg_username")
    keep = {k: v for k, v in (query or {}).items() if k in BOT_QUERY_KEYS}
    url = f"https://t.me/{username}" + (f"?{urlencode(keep)}" if keep else "")
    return Link(url=url, kind="tg_username", username=username.lower())


def _normalize_tg(host: str, path: str, query_string: str) -> Link:
    if host.endswith(".t.me") and host not in TG_HOSTS:
        return _tg_username_link(host[: -len(".t.me")])
    segments = [s for s in path.split("/") if s]
    query = dict(parse_qsl(query_string))
    if not segments:
        raise LinkError("tg_empty")
    first = segments[0]
    if first.startswith("+"):
        invite = first[1:]
        if not INVITE_RE.match(invite):
            raise LinkError("tg_invite")
        return Link(url=f"https://t.me/+{invite}", kind="tg_invite", invite=invite)
    lowered = first.lower()
    if lowered == "joinchat":
        if len(segments) < 2 or not INVITE_RE.match(segments[1]):
            raise LinkError("tg_invite")
        return Link(url=f"https://t.me/+{segments[1]}", kind="tg_invite", invite=segments[1])
    if lowered == "c":
        if len(segments) >= 3 and segments[1].isdigit() and segments[2].isdigit():
            return Link(
                url=f"https://t.me/c/{segments[1]}/{segments[2]}", kind="tg_private", post_id=int(segments[2])
            )
        raise LinkError("tg_private")
    if lowered == "s" and len(segments) >= 2:
        segments = segments[1:]
        first = segments[0]
        lowered = first.lower()
    if lowered in RESERVED_TG_PATHS:
        url = "https://t.me/" + "/".join(segments) + (f"?{query_string}" if query_string else "")
        return Link(url=url, kind="tg_other")
    if not USERNAME_RE.match(first):
        raise LinkError("tg_username")
    if len(segments) >= 2 and segments[1].isdigit():
        return Link(
            url=f"https://t.me/{first}/{segments[1]}",
            kind="tg_post",
            username=first.lower(),
            post_id=int(segments[1]),
        )
    if len(segments) >= 2:
        # Mini App link t.me/<bot>/<app>: the bot username decides whether it is alive
        keep = {k: v for k, v in query.items() if k in BOT_QUERY_KEYS}
        url = f"https://t.me/{first}/{segments[1]}" + (f"?{urlencode(keep)}" if keep else "")
        return Link(url=url, kind="tg_username", username=first.lower())
    return _tg_username_link(first, query)


def normalize(raw: str) -> Link:
    value = (raw or "").strip()
    if not value:
        raise LinkError("empty")
    if len(value) > 512:
        raise LinkError("too_long")
    if has_forbidden_chars(value) or any(c.isspace() for c in value):
        raise LinkError("bad_chars")
    if value.startswith("@"):
        return _tg_username_link(value[1:])
    lowered = value.lower()
    if lowered.startswith("tg://"):
        parts = urlsplit(value)
        query = dict(parse_qsl(parts.query))
        action = (parts.netloc or parts.path).lower()
        if action == "resolve" and query.get("domain"):
            return _tg_username_link(query["domain"], query)
        if action == "join" and query.get("invite"):
            invite = query["invite"]
            if not INVITE_RE.match(invite):
                raise LinkError("tg_invite")
            return Link(url=f"https://t.me/+{invite}", kind="tg_invite", invite=invite)
        raise LinkError("scheme")
    if "://" not in value:
        if _BARE_SCHEME_RE.match(value):  # javascript:, mailto:, data: ...
            raise LinkError("scheme")
        value = "https://" + value
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise LinkError("scheme")
    if parts.username or parts.password:
        raise LinkError("credentials")
    try:
        host = (parts.hostname or "").lower().rstrip(".")  # "t.me." is t.me, "site.com." is site.com
        port = parts.port
    except ValueError as exc:
        raise LinkError("host") from exc
    if not host or "." not in host:
        raise LinkError("host")
    if host in TG_HOSTS or host.endswith(".t.me"):
        return _normalize_tg(host, parts.path, parts.query)
    if scheme != "https":
        raise LinkError("https_only")
    if _is_ip(host) or host.endswith(LOCAL_SUFFIXES) or port not in (None, 443):
        raise LinkError("host")  # an address or a port of some network, not a public site
    try:
        host_idna = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise LinkError("host") from exc
    netloc = host_idna
    url = urlunsplit(("https", netloc, parts.path or "", parts.query, ""))
    return Link(url=url, kind="external", host=host_idna)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _bare_host(link: Link) -> str:
    host = (link.host or urlsplit(link.url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _page(link: Link) -> str:
    """The page an external link opens, whatever query, fragment, "www." or trailing slash it carries."""
    path = urlsplit(link.url).path.rstrip("/").lower()
    return _bare_host(link) + path


def blacklist_keys(link: Link) -> set[tuple[str, str]]:
    """What makes two links the same target (duplicates, reports, bans of that exact place)."""
    keys: set[tuple[str, str]] = set()
    if link.username:
        keys.add(("username", link.username))
    if link.kind == "tg_invite" and link.invite:
        keys.add(("url", f"https://t.me/+{link.invite}"))
    if link.kind == "external":
        keys.add(("url", link.url.rstrip("/").lower()))  # as stored by older versions
        keys.add(("page", _page(link)))
    if link.kind in ("tg_other", "tg_private"):
        keys.add(("url", link.url.lower()))
    return keys


def ban_keys(link: Link) -> set[tuple[str, str]]:
    """What a ban of this link stores: the page, and the whole site when the link is the site itself (a page
    on a shared host, like a link-in-bio service, bans that page only)."""
    keys = blacklist_keys(link)
    if link.kind == "external" and not urlsplit(link.url).path.strip("/"):
        keys.add(("host", _bare_host(link)))
    return keys


def check_keys(link: Link) -> set[tuple[str, str]]:
    """What a new link is checked against the blacklist with: its own keys and its site."""
    keys = blacklist_keys(link)
    if link.kind == "external":
        keys.add(("host", _bare_host(link)))
    return keys


def same_target(a: Link, b: Link) -> bool:
    return bool(blacklist_keys(a) & blacklist_keys(b))


def try_normalize(raw: str) -> Link | None:
    try:
        return normalize(raw)
    except LinkError:
        return None
