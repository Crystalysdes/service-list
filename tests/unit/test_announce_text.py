from __future__ import annotations

from app.services.announce import short


def test_a_long_description_is_cut_at_a_word():
    assert short("  Дешёвые   авиабилеты\n\nпо всему миру  ") == "Дешёвые авиабилеты по всему миру"
    text = "слово " * 100
    cut = short(text, limit=50)
    assert len(cut) <= 50 and cut.endswith("слово…")
    assert short(None) == ""
