"""The texts of the bot's news in the Service List Info channel."""

from __future__ import annotations

from app.db.models import Category, Feature, ScamEntry, Service
from app.domain.render import RenderTemplates
from app.domain.richtext import Fragment, RichText
from app.domain.symbols import LinkContext
from app.services import infofeed
from app.services.settings import Templates

TPL = RenderTemplates.from_settings(Templates(), garant=True)


def _category(**kw) -> Category:
    fields = {
        "id": 7,
        "slug": "travel",
        "title": "Travel [путешествия]",
        "nav_label": "#travel",
        "is_open": True,
    }
    return Category(**{**fields, **kw})


def _service(**kw) -> Service:
    fields = {
        "id": 12,
        "name": "Sky Tours",
        "url": "https://t.me/skytours",
        "url_kind": "telegram",
        "description": "Туры\nпо всему миру, " + "очень " * 80 + "дёшево.",
    }
    return Service(**{**fields, **kw})


def _text(content: dict) -> str:
    return Fragment.from_json(content["fragment"]).text


def _links(content: dict) -> list[str]:
    return [e.url for e in Fragment.from_json(content["fragment"]).entities if e.type == "text_link"]


def test_a_new_service_links_to_its_category_and_lets_others_add_theirs():
    content = infofeed._service_news(_service(), _category(), TPL, {})
    text = _text(content)
    assert text.startswith(
        "🆕 Новый сервис в Service List\n\nSky Tours\n📂 Travel [путешествия]\n\nТуры по всему"
    )
    assert text.endswith("…\n\n#новый_сервис") and len(text) < 500  # the description is cut at a word
    assert _links(content) == ["https://t.me/skytours", "post:cat:7"]  # its name as in the list
    assert "plain" not in content and "linked" not in content  # nothing premium in it
    assert content["buttons"] == [
        ["🔗 Открыть", "https://t.me/skytours"],
        ["📋 В списке", "post:cat:7"],
        ["➕ Добавить свой сервис", "bot:start:add_travel"],
    ]
    closed = infofeed._service_news(
        _service(url_kind="note", url="", description=None), _category(is_open=False), TPL, {}
    )
    assert closed["buttons"] == [["📋 В списке", "post:cat:7"]]  # no link to open, no places to take
    assert "\n\n#новый_сервис" in _text(closed)


def test_a_new_category_invites_to_take_a_place_while_it_is_open():
    content = infofeed._category_news(_category(), {})
    assert _text(content) == (
        "📂 Новая категория в Service List\n\nTravel [путешествия]  #travel\n\n"
        "Места открыты — добавьте свой сервис первым.\n\n#новая_категория"
    )
    assert content["buttons"] == [
        ["📂 Открыть категорию", "post:cat:7"],
        ["➕ Занять место", "bot:start:add_travel"],
    ]
    closed = infofeed._category_news(_category(is_open=False), {})
    assert "Места открыты" not in _text(closed) and len(closed["buttons"]) == 1


def test_a_confirmed_owner_is_not_named():
    content = infofeed._claim_news(_service(owner_id=4242), _category(), TPL, {})
    text = _text(content)
    assert text.startswith("✅ Владелец подтвердил сервис\n\nSky Tours\n📂 Travel") and "#подтверждён" in text
    assert "4242" not in text


def test_a_deal_is_only_the_fact_and_its_ordinal():
    content = infofeed._deal_news(57, {})
    text = _text(content)
    assert text.startswith("🛡 Успешная сделка через Авто-гарант №57\n\n") and text.endswith("#гарант")
    assert not any(ch.isdigit() for ch in text.replace("57", ""))  # no sum, no deal number
    assert content["buttons"] == [["🛡 Сделка через гаранта", "bot:start:garant"]]


def test_a_scam_entry_shows_its_link_as_code_and_leads_to_the_card():
    entry = ScamEntry(
        id=3,
        name="Fake Tours",
        url="https://t.me/faketours",
        category_title="Travel",
        category_label="#travel",
        summary="Взяли\n\nпредоплату " * 100,
    )
    content = infofeed._scam_news(entry, Templates(), {})
    fragment = Fragment.from_json(content["fragment"])
    assert fragment.text.startswith(
        "🚫 Новая запись в Scam list\n\nFake Tours\nСсылка: https://t.me/faketours\nВетка: Travel #travel\n\n"
    )
    code = [fragment.entity_text(e) for e in fragment.entities if e.type == "code"]
    assert code == ["https://t.me/faketours"] and not _links(content)  # not clickable
    assert len(fragment.text) < 800 and fragment.text.endswith("#scam")
    assert content["buttons"] == [
        ["📸 Карточка и скриншоты", "scam:card:3"],
        ["🚫 Весь Scam list", "channel:scam"],
    ]


def test_symbolic_links_resolve_into_the_main_channel():
    ctx = LinkContext(
        bot_username="servicelist_bot",
        post_base="https://t.me/servicelist/",
        posts={"cat:7": 41},
        channels={"scam": "https://t.me/slscam"},
    )
    content = infofeed._service_news(_service(), _category(), TPL, {})
    fragment = Fragment.from_json(content["fragment"]).map_links(ctx.resolve)
    assert [e.url for e in fragment.entities if e.type == "text_link"] == [
        "https://t.me/skytours",
        "https://t.me/servicelist/41",
    ]
    assert [ctx.resolve(url) for _, url in content["buttons"]] == [
        "https://t.me/skytours",
        "https://t.me/servicelist/41",
        "https://t.me/servicelist_bot?start=add_travel",
    ]


def _header() -> dict:
    """A category header as the list shows it: a premium emoji, the name in bold, another emoji at the end."""
    return (
        RichText()
        .emoji("5001", "✈️")
        .text(" ")
        .text("Travel [путешествия]", "bold")
        .text(" ")
        .emoji("5002", "🌍")
        .build()
        .to_json()
    )


def _premium_service() -> Service:
    service = _service()
    service.features = [
        Feature(kind="emoji", status="active", params={"emoji_id": "6001", "alt": "⭐"}),
        Feature(
            kind="font",
            status="active",
            params={"glyphs": [["7001", "S"], ["7002", "k"], [None, " "], ["7003", "y"]]},
        ),
    ]
    return service


def test_the_category_keeps_its_premium_emoji_outside_the_link_to_its_post():
    line = infofeed.category_line(_category(header=_header()))
    assert line.text == "✈️ Travel [путешествия] 🌍"
    emoji = [e for e in line.entities if e.type == "custom_emoji"]
    links = [e for e in line.entities if e.type == "text_link"]
    assert [e.custom_emoji_id for e in emoji] == ["5001", "5002"]
    assert [line.entity_text(e) for e in links] == ["Travel [путешествия]"] and links[0].url == "post:cat:7"
    assert not any(k.offset <= e.offset < k.end for e in emoji for k in links)  # Telegram would drop them
    assert not [e for e in line.entities if e.type == "bold"]  # the header's styles stay in the list
    assert infofeed.category_line(_category(header=None)).text == "📂 Travel [путешествия]"
    assert infofeed.category_line(_category(header=None), folder=False).text == "Travel [путешествия]"
    own = RichText().text("🗺️Travel [путешествия]", "bold").build().to_json()  # its own plain emoji
    assert infofeed.category_line(_category(header=own)).text == "🗺️Travel [путешествия]"


def test_news_with_premium_emoji_has_a_version_without_them_and_one_for_the_account():
    icons = {infofeed.SERVICE: "9001"}
    content = infofeed._service_news(_premium_service(), _category(header=_header()), TPL, icons)
    full = Fragment.from_json(content["fragment"])
    ids = [e.custom_emoji_id for e in full.sorted_entities() if e.type == "custom_emoji"]
    # the icon set on the Info screen, the service's emoji, its glowing name, the category's emoji
    assert ids == ["9001", "6001", "7001", "7002", "7003", "5001", "5002"]
    assert full.text.startswith("🆕 Новый сервис в Service List\n\n⭐")
    assert "[тык.]" in full.text  # the glowing name's link is next to it
    plain = Fragment.from_json(content["plain"])
    assert not plain.custom_emoji_count()
    assert plain.text.startswith("🆕 Новый сервис в Service List\n\n⭐Sky Tours\n✈️ Travel [путешествия] 🌍")
    assert "[тык.]" not in plain.text  # the glowing name is written as the name, itself the link
    linked = Fragment.from_json(content["linked"])
    link = next(e for e in linked.entities if e.type == "text_link")
    assert link.url == "https://t.me/skytours" and linked.entity_text(link) == "Sk y"  # the glowing name
    assert "[тык.]" not in linked.text


def test_the_usual_icon_stays_under_a_premium_one():
    content = infofeed._deal_news(3, {infofeed.DEAL: "9004"})
    fragment = Fragment.from_json(content["fragment"])
    icon = fragment.sorted_entities()[0]
    assert (icon.type, icon.custom_emoji_id, fragment.entity_text(icon)) == ("custom_emoji", "9004", "🛡")
    assert Fragment.from_json(content["plain"]).text == fragment.text  # seen where premium emoji are not
    assert "plain" not in infofeed._deal_news(3, {})
