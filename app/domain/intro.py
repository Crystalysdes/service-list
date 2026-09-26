"""The channel's main post: an updated version that keeps the owner's wording and formatting."""

from __future__ import annotations

from app.domain.richtext import Fragment, RichText

ADD_TEXT = "➕ Хотите добавить свой сервис? Жмите кнопку под постом — заявка через бота за пару минут."


def garant_paragraph(fee_percent: int | float) -> Fragment:
    fee = f"{fee_percent:g}"
    rt = RichText().text("🛡 ")
    rt.link("Auto-garant", "bot:start:garant", "bold")
    rt.text(
        " — безопасные сделки прямо в боте: деньги замораживаются у гаранта, пока обе стороны не "
        f"подтвердят. Спор решает модератор, комиссия {fee}%."
    )
    return rt.build()


def paragraphs(fragment: Fragment) -> list[Fragment]:
    """Blocks of text separated by blank lines, with their formatting."""
    result: list[Fragment] = []
    start = end = None
    for line in fragment.lines():
        if line.text.strip():
            if start is None:
                start = line.start
            end = line.end
        elif start is not None:
            result.append(fragment.slice(start, end))
            start = end = None
    if start is not None:
        result.append(fragment.slice(start, end))
    return result


def _is_links(paragraph: Fragment) -> bool:
    low = paragraph.text.lower()
    return low.startswith(("link:", "chat:", "🔗", "💬")) or "\nchat:" in low or "\nlink:" in low


def updated_intro(current: Fragment, fee_percent: int | float) -> Fragment:
    """The current post plus a paragraph about Auto-garant (before the «⚠️» warning) and the "add your
    service" line pointing at the button under the post. Everything else stays as the owner wrote it."""
    parts = paragraphs(current)
    garant = garant_paragraph(fee_percent)
    add = Fragment.plain(ADD_TEXT)
    has_garant = any("auto-garant" in p.text.lower() for p in parts)
    has_add = False
    out: list[Fragment] = []
    for part in parts:
        low = part.text.lower()
        if not has_garant and (part.text.startswith("⚠") or "ответственност" in low):
            out.append(garant)
            has_garant = True
        if "добавить свой сервис" in low or "добавить сервис" in low:
            out.append(add)
            has_add = True
            continue
        out.append(part)
    missing = ([] if has_garant else [garant]) + ([] if has_add else [add])
    if missing:  # before the links at the end, or at the very end
        index = next((i for i, part in enumerate(out) if _is_links(part)), len(out))
        out[index:index] = missing
    result = Fragment()
    for index, part in enumerate(out):
        if index:
            result = result + Fragment.plain("\n\n")
        result = result + part
    return result
