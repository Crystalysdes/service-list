"""In-memory stand-in for the link checker's HTTP fetcher."""

from __future__ import annotations

from app.domain.linkcheck import Page

USERNAMES = [
    "travelcat_bot",
    "lvtravel",
    "hoteltraffic",
    "hannibal_lecter",
    "luckymantravel",
    "tripmafia",
    "cocojango",
    "luger_ovpn",
    "ventasvpn",
    "hidden_vpn_bot",
    "framestudio",
    "crystalys",
    "kurasao",
    "sirop_design",
    "showydesign",
]


def tme_page(title: str | None, description: str = "") -> Page:
    head = f'<div class="tgme_page_title"><span dir="auto">{title}</span></div>' if title else ""
    about = f'<div class="tgme_page_description" dir="auto">{description}</div>'
    body = f'<div class="tgme_page">{head}{about}</div>'
    return Page(200, body)


class FakeFetcher:
    def __init__(self) -> None:
        self.pages: dict[str, Page] = {}
        self.calls: list[tuple[str, str]] = []
        self.tme("alive_canary", "Alive canary")
        self.tme("dead_canary", None)
        self.pages["HEAD https://alive.example"] = Page(200)
        self.pages["GET https://dead.example"] = Page(None, error="dns")
        for username in USERNAMES:  # the fixture channel: everything alive
            self.tme(username, username.replace("_", " ").title())

    def tme(self, username: str, title: str | None, description: str = "") -> None:
        self.pages[f"https://t.me/{username}"] = tme_page(title, description)

    async def fetch(self, url: str, method: str = "GET") -> Page:
        self.calls.append((method, url))
        return self.pages.get(f"{method} {url}") or self.pages.get(url) or Page(None, error="network")

    async def close(self) -> None:
        return None
