from app.bot.i18n import Translator, missing_keys


def test_locales_have_same_keys():
    assert missing_keys() == {"ru": set(), "en": set()}


def test_translator_fallback_and_format():
    t = Translator("en")
    assert t("captcha.wrong", left=2).startswith("❌")
    assert Translator("xx")("common.back") == "⬅️ Назад"
    assert t("no.such.key") == "no.such.key"


def test_no_key_is_read_as_a_yaml_boolean():
    """Unquoted yes/no/on/off keys turn into True/False and the texts under them are lost."""
    from app.bot.i18n import _catalog

    for lang, flat in _catalog().items():
        assert not [k for k in flat if {"True", "False"} & set(k.split("."))], lang
    t = Translator("ru")
    assert (t("common.yes"), t("common.no"), t("g.err.off")) == (
        "✅ Да",
        "✖️ Нет",
        "Приём новых сделок сейчас приостановлен.",
    )


def test_the_texts_of_a_deal_in_btc_or_ltc_are_whole():
    """A deal in BTC or LTC has texts of its own (the USDT ones name BEP20): every placeholder of them is
    filled for each coin, in both languages."""
    from app.bot.i18n import _catalog
    from app.bot.routers.user.escrow import coin_texts
    from app.services.escrow.money import BTC, LTC

    params = {
        "n": 1,
        "fee": "1",
        "coins": "USDT BEP20, Bitcoin (BTC)",
        "release_hours": 72,
        "grace_hours": 24,
        "usd": "$50",
        "min": "0.0002 BTC",
        "max": "0.1 BTC",
        "min_usd": "$12.60",
        "max_usd": "$6 300",
        "rate": "$63 000",
        "example": "0.0015",
        "amount": "0.001 BTC",
        "buyer_pays": "0.00105 BTC",
        "seller_gets": "0.001 BTC",
        "address": "bc1q…",
        "pay_due": "1.10 12:00",
        "deliver_due": "3.10 12:00",
        "seller_share": "0.0005 BTC",
        "buyer_share": "0.0005 BTC",
        "note": "x",
        "value": "0.001 BTC",
        "title": "t",
    }
    extra = ("g.home_coins", "g.how_coins", "g.card.usd", "g.w.coin", "g.w.no_rate", "g.w.amount_is")
    keys = [
        k
        for k in _catalog()["ru"]
        if (k.startswith("g.") and (k.endswith("_coin") or k.startswith("g.addr.coin_"))) or k in extra
    ]
    assert len(keys) == 24  # a new text of a coin joins the check by its name
    for lang in ("ru", "en"):
        t = Translator(lang)
        for coin in (BTC, LTC):
            for key in keys:
                text = t(key, **params, **coin_texts(t, coin))
                assert "{" not in text and "}" not in text, (lang, coin.code, key)
