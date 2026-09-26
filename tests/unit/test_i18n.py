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
