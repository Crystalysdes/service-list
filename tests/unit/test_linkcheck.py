import pytest

from app.domain.linkcheck import (
    ALIVE,
    DEAD,
    UNKNOWN,
    Page,
    fingerprint,
    http_verdict,
    post_verdict,
    tme_description,
    tme_verdict,
)

# trimmed copies of real t.me pages
CHANNEL_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Telegram: Contact @durov</title>
<meta property="og:title" content="Durov's Channel"></head><body class="no_transition">
<div class="tgme_page_wrap"><div class="tgme_body_wrap"><div class="tgme_page">
<div class="tgme_page_photo"><a href="tg://resolve?domain=durov">
<img class="tgme_page_photo_image" src="x.jpg"></a></div>
<div class="tgme_page_title"><span dir="auto">Durov&#39;s Channel 🔥</span></div>
<div class="tgme_page_extra">9 000 000 subscribers</div>
<div class="tgme_page_description" dir="auto">Thoughts from the CEO of Telegram<br/>SL-1A2B3C4D</div>
<div class="tgme_page_action"><a class="tgme_action_button_new shine" href="tg://resolve?domain=durov">View</a></div>
</div></div></div></body></html>"""

MISSING_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Telegram: Contact @zq9x_nobody</title>
<meta property="og:title" content="Telegram: Contact @zq9x_nobody"></head><body class="no_transition">
<div class="tgme_page_wrap"><div class="tgme_body_wrap"><div class="tgme_page">
<div class="tgme_page_icon"><i class="tgme_icon_user"></i></div>
<div class="tgme_page_description" dir="auto">If you have <strong>Telegram</strong>, you can contact
<a class="tgme_username_link" href="tg://resolve?domain=zq9x_nobody">@zq9x_nobody</a> right away.</div>
<div class="tgme_page_action"><a class="tgme_action_button_new shine" href="tg://resolve?domain=zq9x_nobody">
Send Message</a></div></div></div></div></body></html>"""

POST_PAGE = (
    '<div class="tgme_widget_message_wrap">'
    '<div class="tgme_widget_message js-widget_message" data-post="durov/1">'
)
POST_MISSING = (
    '<div class="tgme_widget_message_wrap"><div class="tgme_widget_message_error">Post not found</div></div>'
)


def test_tme_page_with_title_is_alive():
    verdict = tme_verdict(Page(200, CHANNEL_PAGE))
    assert verdict.state == ALIVE
    assert verdict.title == "Durov's Channel 🔥"
    assert verdict.fingerprint == "durovschannel"


def test_tme_page_without_title_is_dead():
    assert tme_verdict(Page(200, MISSING_PAGE)).state == DEAD


@pytest.mark.parametrize(
    ("page", "state"),
    [
        (Page(None, error="timeout"), UNKNOWN),
        (Page(429, "Too Many Requests"), UNKNOWN),
        (Page(502, "Bad gateway"), UNKNOWN),
        (Page(200, "<html>captcha</html>"), UNKNOWN),
        (Page(404, ""), DEAD),
    ],
)
def test_tme_page_ambiguous_answers(page, state):
    assert tme_verdict(page).state == state


def test_post_widget():
    assert post_verdict(Page(200, POST_PAGE)).state == ALIVE
    assert post_verdict(Page(200, POST_MISSING)).state == DEAD
    assert post_verdict(Page(503, "")).state == UNKNOWN


@pytest.mark.parametrize(
    ("page", "state"),
    [
        (Page(200, ""), ALIVE),
        (Page(301, ""), ALIVE),
        (Page(404, ""), DEAD),
        (Page(410, ""), DEAD),
        (Page(None, error="dns"), DEAD),
        (Page(403, ""), UNKNOWN),
        (Page(429, ""), UNKNOWN),
        (Page(500, ""), UNKNOWN),
        (Page(None, error="tls"), UNKNOWN),
        (Page(None, error="timeout"), UNKNOWN),
        (Page(None, error="network"), UNKNOWN),
    ],
)
def test_http_verdicts(page, state):
    assert http_verdict(page).state == state


def test_fingerprint_ignores_decoration_but_not_renames():
    assert fingerprint("Trip Mafia ✈️") == fingerprint("TRIPMAFIA!")
    assert fingerprint("Trip Mafia") != fingerprint("Crypto Casino")
    assert fingerprint("") is None and fingerprint("🔥🔥") is None


def test_description_extraction():
    assert "SL-1A2B3C4D" in tme_description(CHANNEL_PAGE)
    assert "right away" in tme_description(MISSING_PAGE)
