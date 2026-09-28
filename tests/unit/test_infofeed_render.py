"""The texts of the bot's news in the Service List Info channel."""

from __future__ import annotations

from app.db.models import Category, ScamEntry, Service
from app.domain.richtext import Fragment
from app.domain.symbols import LinkContext
from app.services import infofeed
from app.services.settings import Templates


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
    content = infofeed._service_news(_service(), _category())
    text = _text(content)
    assert text.startswith(
        "🆕 Новый сервис в Service List\n\nSky Tours\n📂 Travel [путешествия]\n\nТуры по всему"
    )
    assert text.endswith("…\n\n#новый_сервис") and len(text) < 500  # the description is cut at a word
    assert _links(content) == ["post:cat:7"]
    assert content["buttons"] == [
        ["🔗 Открыть", "https://t.me/skytours"],
        ["📋 В списке", "post:cat:7"],
        ["➕ Добавить свой сервис", "bot:start:add_travel"],
    ]
    closed = infofeed._service_news(
        _service(url_kind="note", url="", description=None), _category(is_open=False)
    )
    assert closed["buttons"] == [["📋 В списке", "post:cat:7"]]  # no link to open, no places to take
    assert "\n\n#новый_сервис" in _text(closed)


def test_a_new_category_invites_to_take_a_place_while_it_is_open():
    content = infofeed._category_news(_category())
    assert _text(content) == (
        "📂 Новая категория в Service List\n\nTravel [путешествия]  #travel\n\n"
        "Места открыты — добавьте свой сервис первым.\n\n#новая_категория"
    )
    assert content["buttons"] == [
        ["📂 Открыть категорию", "post:cat:7"],
        ["➕ Занять место", "bot:start:add_travel"],
    ]
    closed = infofeed._category_news(_category(is_open=False))
    assert "Места открыты" not in _text(closed) and len(closed["buttons"]) == 1


def test_a_confirmed_owner_is_not_named():
    content = infofeed._claim_news(_service(owner_id=4242), _category())
    text = _text(content)
    assert text.startswith("✅ Владелец подтвердил сервис\n\nSky Tours\n📂 Travel") and "#подтверждён" in text
    assert "4242" not in text


def test_a_deal_is_only_the_fact_and_its_ordinal():
    content = infofeed._deal_news(57)
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
    content = infofeed._scam_news(entry, Templates())
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
    content = infofeed._service_news(_service(), _category())
    fragment = Fragment.from_json(content["fragment"]).map_links(ctx.resolve)
    assert [e.url for e in fragment.entities if e.type == "text_link"] == ["https://t.me/servicelist/41"]
    assert [ctx.resolve(url) for _, url in content["buttons"]] == [
        "https://t.me/skytours",
        "https://t.me/servicelist/41",
        "https://t.me/servicelist_bot?start=add_travel",
    ]
