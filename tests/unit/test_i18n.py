from app.bot.i18n import Translator, missing_keys


def test_locales_have_same_keys():
    assert missing_keys() == {"ru": set(), "en": set()}


def test_translator_fallback_and_format():
    t = Translator("en")
    assert t("captcha.wrong", left=2).startswith("❌")
    assert Translator("xx")("common.back") == "⬅️ Назад"
    assert t("no.such.key") == "no.such.key"
