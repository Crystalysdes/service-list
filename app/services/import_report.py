"""Human-readable import reports and previews."""

from __future__ import annotations

from typing import Any

from app.bot.i18n import h
from app.domain.fonts import glyphs_from_json
from app.domain.import_plan import item_view
from app.domain.render import CategoryView, RenderTemplates, render_category
from app.domain.richtext import Fragment, RichText
from app.domain.symbols import LinkContext, channel_post_base


def summary(report: dict[str, Any]) -> str:
    scan = report.get("scan", {})
    plan = report.get("plan", {})
    stats = plan.get("stats", {})
    channel = plan.get("channel", {})
    lines = [
        f"📦 <b>Импорт канала</b> {h(channel.get('title') or '')}"
        + (f" (@{h(channel['username'])})" if channel.get("username") else ""),
        f"Просканировано постов: {scan.get('found', 0)} (до ID {scan.get('last_id', '?')})",
        "",
    ]
    intro = plan.get("intro")
    if intro:
        lines.append(
            f"• Приветствие: пост #{intro['message_id']}" + (" (с медиа)" if intro.get("media") else "")
        )
    else:
        lines.append("• Приветствие: не найдено")
    lines.append(f"• Категорий: {stats.get('categories', 0)} (сервисов: {stats.get('services', 0)})")
    nav = plan.get("nav")
    if nav:
        lines.append(f"• Навигация: пост #{nav['message_id']}, ссылок: {len(nav['entries'])}")
        if nav.get("missing_targets"):
            lines.append(f"  ⚠️ ссылки на посты без списка: {', '.join(map(str, nav['missing_targets']))}")
        if nav.get("fidelity") and not nav["fidelity"].get("equal"):
            lines.append("  ⚠️ оформление навигации будет слегка отличаться (см. подробный отчёт)")
    else:
        lines.append("• Навигация: ⚠️ не найдена (будет создана заново)")
    lines.append(f"• Статичных постов: {len(plan.get('statics', []))}")
    lines.append(
        f"• Премиум-эмодзи: {stats.get('premium_emoji', 0)}, эмодзи-названий: {stats.get('emoji_names', 0)}"
    )
    if stats.get("raw"):
        lines.append(f"• Строк с нестандартным оформлением: {stats['raw']} (сохранятся как есть)")
    total = stats.get("categories", 0)
    ok = stats.get("fidelity_ok", 0)
    lines.append(f"• Оформление совпадает: {ok}/{total} {'✅' if ok == total else '⚠️'}")
    if plan.get("trailing"):
        lines.append(f"• Постов после навигации: {len(plan['trailing'])} — бот их не трогает")
    if plan.get("unresolved"):
        lines.append("")
        lines.append(
            f"🔤 Нужно указать названия сервисов из эмодзи-букв: {len(plan['unresolved'])} — "
            "в канале они станут обычными названиями"
        )
    lines.append("")
    lines.append("В канале ничего не меняется, пока вы не включите «В эфир».")
    return "\n".join(lines)


def detailed(report: dict[str, Any]) -> str:
    plan = report.get("plan", {})
    out: list[str] = ["ПОДРОБНЫЙ ОТЧЁТ ИМПОРТА", ""]
    for index, category in enumerate(plan.get("categories", [])):
        fid = category["fidelity"]
        mark = "OK" if fid["text_equal"] and fid["structure_equal"] else "ОТЛИЧИЯ"
        out.append(
            f"[{index + 1}] {category['title']}  (пост #{category['message_id']}, slug={category['slug']}, "
            f"навигация={category.get('nav_label') or '—'})  оформление: {mark}"
        )
        if fid.get("first_diff"):
            out.append(f"    ! {fid['first_diff']}")
        for old, new in fid.get("url_changes", []):
            out.append(f"    ссылка: {old} -> {new}")
        for warning in category.get("warnings", []):
            out.append(f"    ! {warning}")
        for item in category["items"]:
            flags = []
            if item.get("emoji"):
                flags.append("эмодзи")
            if item.get("glyphs"):
                flags.append("из эмодзи-букв → обычное название")
            if item["kind"] == "raw":
                flags.append("как есть")
            if item["kind"] == "text":
                flags.append("текст")
            name = item.get("name") or "(название не указано)"
            out.append(
                f"    - {name} | {item.get('url') or '—'}" + (f"  [{', '.join(flags)}]" if flags else "")
            )
            for warning in item.get("warnings", []):
                out.append(f"        ! {warning}")
        out.append("")
    nav = plan.get("nav")
    if nav:
        out.append(f"НАВИГАЦИЯ (пост #{nav['message_id']}):")
        for entry in nav["entries"]:
            out.append(f"    {entry['label']} -> пост #{entry['target']}")
        for warning in nav.get("warnings", []):
            out.append(f"    ! {warning}")
        out.append("")
    for static in plan.get("statics", []):
        text = Fragment.from_json(static["content"]).text.replace("\n", " ")
        out.append(f"СТАТИЧНЫЙ ПОСТ #{static['message_id']}: {text[:120]}")
    if plan.get("trailing"):
        out.append(f"ПОСТЫ ПОСЛЕ НАВИГАЦИИ (не управляются): {plan['trailing']}")
    return "\n".join(out)


def preview_fragments(report: dict[str, Any], bot_username: str | None) -> list[tuple[str, Fragment]]:
    plan = report.get("plan", {})
    templates = plan.get("templates", {})
    from app.services.settings import Templates

    tpl = RenderTemplates.from_settings(Templates.model_validate({**Templates().model_dump(), **templates}))
    channel = plan.get("channel", {})
    posts: dict[str, int] = {}
    if plan.get("nav"):
        posts["nav"] = plan["nav"]["message_id"]
    for index, category in enumerate(plan.get("categories", [])):
        posts[f"cat:{index}"] = category["message_id"]
    ctx = LinkContext(
        bot_username=bot_username,
        post_base=channel_post_base(channel.get("chat_id", 0), channel.get("username")),
        posts=posts,
    )
    result = []
    for index, category in enumerate(plan.get("categories", [])):
        view = CategoryView(
            id=index,
            slug=category["slug"],
            header=Fragment.from_json(category["header"]),
            items=[item_view(item) for item in category["items"]],
        )
        result.append((category["title"], render_category(view, tpl, ctx)))
    return result


def glyph_preview(item: dict[str, Any]) -> Fragment:
    rt = RichText()
    for glyph in glyphs_from_json(item.get("glyphs")):
        if glyph.emoji_id:
            rt.emoji(glyph.emoji_id, glyph.alt)
        else:
            rt.text(glyph.alt)
    return rt.build()
