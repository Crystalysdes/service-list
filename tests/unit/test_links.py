import pytest

from app.domain.links import (
    LinkError,
    blacklist_keys,
    clean_text,
    normalize,
    same_target,
    shorten_name,
    tidy_name,
    tidy_prefix,
    without_emoji,
)
from app.domain.symbols import LinkContext, channel_post_base


@pytest.mark.parametrize(
    ("raw", "url", "kind", "username"),
    [
        ("@FrameStudio", "https://t.me/FrameStudio", "tg_username", "framestudio"),
        ("t.me/FrameStudio", "https://t.me/FrameStudio", "tg_username", "framestudio"),
        ("http://telegram.me/FrameStudio/", "https://t.me/FrameStudio", "tg_username", "framestudio"),
        ("https://t.me/s/news_channel", "https://t.me/news_channel", "tg_username", "news_channel"),
        (
            "https://t.me/some_bot?start=ref123&utm=x",
            "https://t.me/some_bot?start=ref123",
            "tg_username",
            "some_bot",
        ),
        (
            "tg://resolve?domain=some_bot&start=abc",
            "https://t.me/some_bot?start=abc",
            "tg_username",
            "some_bot",
        ),
        ("https://t.me/chan/123", "https://t.me/chan/123", "tg_post", "chan"),
        ("https://t.me/c/1234567/89", "https://t.me/c/1234567/89", "tg_private", None),
        ("https://t.me/+AbCdEf123", "https://t.me/+AbCdEf123", "tg_invite", None),
        ("https://t.me/joinchat/AbCdEf123", "https://t.me/+AbCdEf123", "tg_invite", None),
        ("durov.t.me", "https://t.me/durov", "tg_username", "durov"),
        (
            "https://t.me/somebot/app?startapp=1",
            "https://t.me/somebot/app?startapp=1",
            "tg_username",
            "somebot",
        ),
        ("https://Example.COM/path?q=1#frag", "https://example.com/path?q=1", "external", None),
        ("example.com", "https://example.com", "external", None),
        ("https://t.me/addemoji/SomePack", "https://t.me/addemoji/SomePack", "tg_other", None),
    ],
)
def test_normalize(raw, url, kind, username):
    link = normalize(raw)
    assert link.url == url
    assert link.kind == kind
    assert link.username == username


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "empty"),
        ("javascript:alert(1)", "scheme"),
        ("ftp://example.com", "scheme"),
        ("http://example.com", "https_only"),
        ("https://user:pass@example.com", "credentials"),
        ("https://t.me/ab", "tg_username"),
        ("https://exa​mple.com", "bad_chars"),
        ("https://example.com/a b", "bad_chars"),
        ("localhost", "host"),
        ("@1abc", "tg_username"),
    ],
)
def test_normalize_rejects(raw, code):
    with pytest.raises(LinkError) as err:
        normalize(raw)
    assert err.value.code == code


def test_blacklist_keys_and_same_target():
    a = normalize("@Scammer_Bot")
    b = normalize("https://t.me/scammer_bot?start=x")
    assert ("username", "scammer_bot") in blacklist_keys(a)
    assert same_target(a, b)
    c = normalize("https://site.com/")
    d = normalize("https://SITE.com")
    assert same_target(c, d)


def test_clean_text():
    assert clean_text("  Frame Studio ") == "Frame Studio"
    with pytest.raises(LinkError):
        clean_text("bad‮text")
    with pytest.raises(LinkError):
        clean_text("two\nlines")
    assert clean_text("two\nlines", allow_newlines=True) == "two\nlines"


@pytest.mark.parametrize(
    "value",
    [
        "Shop\u2028✅ Проверен",  # a line separator: the name would pose as two lines
        "Shop\u2029✅",
        "soft\u00adhyphen",
        "\u3164\u3164",  # Hangul fillers: a blank name
        "tag\U000e0041",
        "mark\u061c",
        "braille\u2800blank",
    ],
)
def test_invisible_characters_are_refused(value):
    with pytest.raises(LinkError):
        clean_text(value, allow_newlines=True)


def test_ordinary_names_still_pass():
    for value in ("Café Délice", "Кофе ☕ 24/7", "🇷🇺 Russia", "Frame-Studio_2"):
        assert clean_text(value) == value


def test_link_context_resolution():
    ctx = LinkContext(
        bot_username="list_bot",
        post_base=channel_post_base(-1001234567890, None),
        posts={"nav": 42, "cat:3": 7},
        channels={"main": "https://t.me/servicelist"},
    )
    assert ctx.resolve("post:nav") == "https://t.me/c/1234567890/42"
    assert ctx.resolve("post:cat:3") == "https://t.me/c/1234567890/7"
    assert ctx.resolve("post:cat:9") is None
    assert ctx.resolve("bot:start:add_{slug}", slug="design") == "https://t.me/list_bot?start=add_design"
    assert ctx.resolve("service:url", service_url="https://t.me/x") == "https://t.me/x"
    assert ctx.resolve("channel:main") == "https://t.me/servicelist"
    assert ctx.resolve("https://example.com") == "https://example.com"
    assert channel_post_base(-100555, "MyChan") == "https://t.me/MyChan/"


@pytest.mark.parametrize(
    ("typed", "kept", "dropped"),
    [
        ("🔥Fly Cheap✈️", "Fly Cheap", True),
        ("Магазин №1 😀", "Магазин №1", True),  # "№" is text
        ("Shop 1️⃣", "Shop 1", True),  # a keycap leaves its digit
        ("👨\u200d💻 Dev team", "Dev team", True),  # a joined emoji goes whole
        ("🇷🇺 Прокси", "Прокси", True),  # a flag
        ("Temp 20° ™ A→B", "Temp 20° ™ A→B", False),  # symbols that are text stay
        ("⭐️⭐️", "", True),
        ("Plain name", "Plain name", False),
        ("FRAUD💳ENROLL", "FRAUD ENROLL", True),  # a space keeps the words apart
    ],
)
def test_emoji_are_dropped_from_names(typed, kept, dropped):
    assert without_emoji(typed) == (kept, dropped)


def test_a_name_as_the_channel_shows_it_and_the_start_of_its_line():
    assert tidy_name("Ракета 🐾 Rocket 🚀") == "Ракета Rocket"
    assert tidy_name("\u2800Spike\u200b ") == "Spike"  # a blank filler and a zero-width space
    assert tidy_name("  Daddy\u00a0 Goose ") == "Daddy Goose"
    assert tidy_name("🚀") == "🚀"  # nothing else: the name stays as it is
    assert tidy_prefix("      ↳  ") == " ↳ "  # the arrow at the edge, the name right after it
    assert tidy_prefix("\u3164\u00a0↳\u2002") == " ↳ "  # fillers and odd spaces: plain ones
    assert tidy_prefix("• ↳ ") == "• ↳ "  # not a single arrow: left as it is


def test_a_name_is_cut_to_one_line_of_a_phone():
    assert shorten_name("Crystalys") == "Crystalys"
    assert shorten_name("Ded CC | Buy credit card") == "Ded CC | Buy credit"  # at a space, no «|» left
    assert shorten_name("Travel with Coco Jango") == "Travel with Coco"
    assert shorten_name("Supercalifragilisticexpialidocious") == "Supercalifragilistic"  # one long word
    assert shorten_name("A B C D E F G H I J K L") == "A B C D E F G H I J"
