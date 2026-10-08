"""Plain emoji at the start of the bot's lines and buttons become its animated icons."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain import iconize

TABLE = iconize.Table({"📋": "11", "✅": "22", "⚠": "33", "🔄": "44", "👍": "55"})


def test_icons_go_at_the_start_of_lines_and_after_opening_tags():
    text = (
        "<b>📋 Service List</b>\nПроверено ✅ всё.\n  ✅ Готово\n"
        "<blockquote>⚠️ Ссылки живут минуту</blockquote>"
    )
    out, count = iconize.html(text, TABLE)
    assert count == 3
    assert out.startswith('<b><tg-emoji emoji-id="11">📋</tg-emoji> Service List</b>')
    assert "Проверено ✅ всё." in out  # inside a sentence: as it was
    assert '\n  <tg-emoji emoji-id="22">✅</tg-emoji> Готово' in out
    assert '<blockquote><tg-emoji emoji-id="33">⚠️</tg-emoji> Ссылки' in out  # its U+FE0F stays inside


def test_no_icons_inside_links_code_or_other_emoji():
    text = (
        '<a href="https://t.me/x">✅ ссылка</a>\n<code>🔄 код</code>\n'
        '<tg-emoji emoji-id="9">✅</tg-emoji> своё'
    )
    assert iconize.html(text, TABLE) == (text, 0)
    # an emoji that goes on into another one (a skin tone) is not the table's
    assert iconize.html("👍🏽 отлично", TABLE) == ("👍🏽 отлично", 0)


def test_a_text_takes_a_limited_number_of_icons():
    text = "\n".join(["✅ пункт"] * (iconize.MAX_PER_TEXT + 5))
    _out, count = iconize.html(text, TABLE)
    assert count == iconize.MAX_PER_TEXT
    # a text with much formatting already: the icons leave room for it within Telegram's 100 entities
    text = "\n".join(["✅ <b>пункт</b>"] * 60)
    _out, count = iconize.html(text, TABLE)
    assert count == iconize.ENTITIES_MAX - 60


def test_buttons_get_the_icon_in_place_of_their_emoji():
    assert iconize.label("🔄 Новые ссылки", TABLE) == ("Новые ссылки", "44")
    assert iconize.label("⚠️ Report service", TABLE) == ("Report service", "33")
    assert iconize.label("🔄", TABLE) is None  # an emoji alone stays (a captcha button)
    assert iconize.label("🍎 Яблоко", TABLE) is None
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Service List", url="https://t.me/x", style="primary")],
            [InlineKeyboardButton(text="✅ Готово", callback_data="ok", icon_custom_emoji_id="7")],
        ]
    )
    new, count = iconize.keyboard(markup, TABLE)
    assert count == 1
    first, second = new.inline_keyboard[0][0], new.inline_keyboard[1][0]
    assert (first.text, first.icon_custom_emoji_id, first.style) == ("Service List", "11", "primary")
    assert (second.text, second.icon_custom_emoji_id) == ("✅ Готово", "7")  # its own icon is kept
    assert markup.inline_keyboard[0][0].text == "📋 Service List"  # the original is not changed
    assert iconize.keyboard(InlineKeyboardMarkup(inline_keyboard=[]), TABLE)[1] == 0
