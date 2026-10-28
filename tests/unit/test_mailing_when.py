"""The time an admin sets a mailing for, typed in the bot's time zone (Moscow here: UTC+3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.mailing import parse_when

NOW = datetime(2026, 10, 28, 12, 30, tzinfo=UTC)  # 15:30 in Moscow
TZ = "Europe/Moscow"


@pytest.mark.parametrize(
    ("text", "moment"),
    [
        ("18:00", datetime(2026, 10, 28, 15, 0, tzinfo=UTC)),  # today
        ("15:30", datetime(2026, 10, 29, 12, 30, tzinfo=UTC)),  # passed this minute: tomorrow
        ("  9:05 ", datetime(2026, 10, 29, 6, 5, tzinfo=UTC)),
        ("завтра 10:00", datetime(2026, 10, 29, 7, 0, tzinfo=UTC)),
        ("Сегодня 23:59", datetime(2026, 10, 28, 20, 59, tzinfo=UTC)),
        ("29.10 18:00", datetime(2026, 10, 29, 15, 0, tzinfo=UTC)),
        ("29.10.2026 18:00", datetime(2026, 10, 29, 15, 0, tzinfo=UTC)),
        ("1.11.26 8:00", datetime(2026, 11, 1, 5, 0, tzinfo=UTC)),
    ],
)
def test_a_time_is_read_in_the_bots_time_zone(text, moment):
    assert parse_when(text, TZ, NOW) == moment


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("сегодня 11:00", "уже прошло"),
        ("27.10 18:00", "уже прошло"),
        ("27.10.2026 18:00", "уже прошло"),
        ("01.03.2027 10:00", "не дальше чем на 60 дней"),
        ("31.02 10:00", "Такой даты"),
        ("25:00", "Такой даты"),
        ("в пятницу", "Не понял"),
        ("", "Не понял"),
    ],
)
def test_a_wrong_time_says_what_is_wrong(text, reason):
    result = parse_when(text, TZ, NOW)
    assert isinstance(result, str) and reason in result


def test_a_date_early_next_year_is_next_years():
    december = datetime(2026, 12, 20, 9, 0, tzinfo=UTC)
    assert parse_when("05.01 10:00", TZ, december) == datetime(2027, 1, 5, 7, 0, tzinfo=UTC)
