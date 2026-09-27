"""The link checker only ever talks to the public internet: a submitted link or a redirect cannot send it into
the bot's own network (the database, 127.0.0.1, cloud metadata)."""

from __future__ import annotations

import socket

import aiohttp
import pytest
from yarl import URL

from app.domain.links import check_keys, normalize, try_normalize
from app.services.linkcheck import HttpFetcher, PublicResolver, public_address, unsafe_hop


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5:6379/",
        "https://db.:5432/",
        "https://localhost./",
        "https://[::1]/",
        "https://printer.local/",
        "https://site.com:8443/",
    ],
)
def test_links_into_a_private_network_are_refused(url):
    assert try_normalize(url) is None


def test_public_links_and_telegram_spellings_still_work():
    assert normalize("https://site.com:443/a").url == "https://site.com/a"
    assert normalize("https://t.me./scamshop").url == "https://t.me/scamshop"
    assert normalize("https://www.telegram.dog/scamshop").username == "scamshop"


def test_a_banned_site_is_found_under_any_spelling():
    banned = check_keys(normalize("https://scam.com"))
    for variant in ("https://scam.com/?a=1", "https://www.scam.com/x", "https://SCAM.com."):
        assert ("host", "scam.com") in banned & check_keys(normalize(variant))


def test_redirect_hops_are_checked():
    assert unsafe_hop(URL("http://127.0.0.1:80/"))
    assert unsafe_hop(URL("http://[::ffff:10.0.0.1]/"))
    assert unsafe_hop(URL("http://example.com:6379/"))
    assert unsafe_hop(URL("ftp://example.com/"))
    assert unsafe_hop(URL("http://localhost/"))
    assert not unsafe_hop(URL("http://example.com/next"))
    assert public_address("8.8.8.8") and not public_address("192.168.1.1")
    assert not public_address("::ffff:127.0.0.1") and not public_address("fe80::1%eth0")


def _answer(host: str, address: str, port: int, family: int) -> dict:
    return {"hostname": host, "host": address, "port": port, "family": family, "proto": 0, "flags": 0}


async def test_names_resolving_into_a_private_network_are_not_reached(monkeypatch):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [_answer(host, "10.1.2.3", port, family), _answer(host, "93.184.216.34", port, family)]

    monkeypatch.setattr(aiohttp.ThreadedResolver, "resolve", resolve)
    found = await PublicResolver().resolve("mixed.example", 443)
    assert [item["host"] for item in found] == ["93.184.216.34"]

    async def private_only(self, host, port=0, family=socket.AF_INET):
        return [_answer(host, "127.0.0.1", port, family)]

    monkeypatch.setattr(aiohttp.ThreadedResolver, "resolve", private_only)
    with pytest.raises(OSError):
        await PublicResolver().resolve("inside.example", 443)


async def test_the_fetcher_refuses_an_internal_address_without_connecting():
    fetcher = HttpFetcher()
    try:
        page = await fetcher.fetch("http://127.0.0.1:8080/admin")
    finally:
        await fetcher.close()
    assert page.status is None and page.error == "blocked"
