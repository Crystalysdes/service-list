"""Catalog operations shared by admin screens and user flows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, Feature, Service
from app.domain.links import NAME_MAX, normalize, shorten_name, tidy_name, tidy_prefix
from app.domain.parse import make_slug
from app.domain.richtext import Entity, Fragment
from app.services.settings import Limits, Templates, get_settings, update_settings


def style_fragment(fragment: Fragment, styles: list[str]) -> Fragment:
    """Wrap a whole fragment with the given entity types (e.g. blockquote + bold for a header)."""
    fragment = fragment.strip()
    length = fragment.u16len
    if not length:
        return fragment
    extra = tuple(Entity(style, 0, length) for style in styles if style)
    existing = tuple(e for e in fragment.entities if e.type not in styles)
    return Fragment(fragment.text, existing + extra)


async def tidy_names(session: AsyncSession) -> None:
    """Services' names without emoji or invisible characters and on one line of a phone, every line's arrow
    at the edge (what migration 0014 did, done again to a restored archive made before it)."""
    changed = set()
    for service in (await session.execute(select(Service))).scalars():
        tidy = shorten_name(tidy_name(service.name))
        if tidy != service.name:
            service.name = tidy
            changed.add(service.id)
    if changed:
        features = await session.execute(
            select(Feature).where(Feature.service_id.in_(changed), Feature.kind == "font")
        )
        for feature in features.scalars():
            if (feature.params or {}).get("glow"):  # drawn again with the new name: its owner is not told
                feature.params = {**feature.params, "glow_quiet": True}
    templates = await get_settings(session, Templates)
    if tidy_prefix(templates.item_prefix) != templates.item_prefix:
        await update_settings(session, Templates, item_prefix=tidy_prefix(templates.item_prefix))
    limits = await get_settings(session, Limits)
    if limits.max_name_len > NAME_MAX:
        await update_settings(session, Limits, max_name_len=NAME_MAX)


def request_sync(ctx: AppContext) -> None:
    engine = ctx.get("sync")
    if engine is not None:
        engine.wake()


async def create_category(session: AsyncSession, header: Fragment, nav_label: str) -> Category:
    templates = await get_settings(session, Templates)
    header = style_fragment(header.without_auto(), templates.header_styles)
    taken = set((await session.execute(select(Category.slug))).scalars())
    label = nav_label.strip()
    if label and not label.startswith("#"):
        label = "#" + label
    slug = make_slug(label, header.text, taken)
    post_order = (await session.scalar(select(func.max(Category.post_order)))) or 0
    from app.db.models import StaticPost

    static_order = (await session.scalar(select(func.max(StaticPost.post_order)))) or 0
    nav_order = (await session.scalar(select(func.max(Category.nav_order)))) or 0
    category = Category(
        slug=slug,
        title=" ".join(header.text.split())[:128],
        nav_label=label[:64],
        header=header.to_json(),
        post_order=max(post_order, static_order) + 1,
        nav_order=min(nav_order + 1, 999),
        top_slots=3,
    )
    session.add(category)
    await session.flush()
    return category


async def set_header(session: AsyncSession, category: Category, header: Fragment) -> None:
    templates = await get_settings(session, Templates)
    styled = style_fragment(header.without_auto(), templates.header_styles)
    category.header = styled.to_json()
    category.title = " ".join(styled.text.split())[:128]


async def move_nav(session: AsyncSession, category: Category, direction: int) -> None:
    rows = list((await session.execute(select(Category).order_by(Category.nav_order, Category.id))).scalars())
    for index, row in enumerate(rows):
        row.nav_order = index
    index = rows.index(category)
    other = index + direction
    if 0 <= other < len(rows):
        rows[index].nav_order, rows[other].nav_order = rows[other].nav_order, rows[index].nav_order


async def next_position(session: AsyncSession, category_id: int) -> int:
    current = await session.scalar(
        select(func.max(Service.position)).where(Service.category_id == category_id)
    )
    return (current or 0) + 1


async def add_service(
    session: AsyncSession,
    category_id: int,
    name: str,
    url: str,
    *,
    owner_id: int | None = None,
    source: str = "admin",
    status: str = "active",
    description: str | None = None,
) -> Service:
    link = normalize(url)
    service = Service(
        category_id=category_id,
        owner_id=owner_id,
        name=name.strip()[:128],
        description=description,
        url=link.url,
        url_kind=link.kind,
        position=await next_position(session, category_id),
        status=status,
        source=source,
        published_at=utcnow() if status == "active" else None,
        extra={},
    )
    session.add(service)
    await session.flush()
    return service


async def move_position(session: AsyncSession, service: Service, direction: int) -> None:
    rows = list(
        (
            await session.execute(
                select(Service)
                .where(Service.category_id == service.category_id, Service.status == "active")
                .order_by(Service.position, Service.id)
            )
        ).scalars()
    )
    for index, row in enumerate(rows):
        row.position = index
    if service not in rows:
        return
    index = rows.index(service)
    other = index + direction
    if 0 <= other < len(rows):
        rows[index].position, rows[other].position = rows[other].position, rows[index].position


async def move_to_category(session: AsyncSession, service: Service, category_id: int) -> list[str]:
    notes = []
    service.category_id = category_id
    service.position = await next_position(session, category_id)
    for feature in service.features:
        feature.category_id = category_id
        if feature.kind == "top" and feature.status == "active":
            taken = await session.scalar(
                select(func.count())
                .select_from(Feature)
                .where(
                    Feature.category_id == category_id,
                    Feature.kind == "top",
                    Feature.status == "active",
                    Feature.top_position == feature.top_position,
                    Feature.id != feature.id,
                )
            )
            if taken:
                feature.status = "revoked"
                notes.append(f"топ-{feature.top_position} занят в новой ветке — топ снят")
    return notes


def service_badges(service: Service) -> str:
    badges = []
    for feature in service.features:
        if feature.status != "active":
            continue
        if feature.kind == "top":
            badges.append(f"⭐{feature.top_position}")
        elif feature.kind == "emoji":
            badges.append("😀")
        elif feature.kind == "font":
            badges.append("🌟")
    if service.status != "active":
        badges.append(
            {"hidden": "🙈", "banned": "🚫", "pending": "⏳", "approved": "💳"}.get(service.status, "·")
        )
    return " ".join(badges)


def feature_line(feature: Any, tz_format: Any) -> str:
    names = {"top": "Топ", "emoji": "Премиум-эмодзи", "font": "Светящийся ник"}
    title = names.get(feature.kind, feature.kind)
    if feature.kind == "top":
        title += f"-{feature.top_position}"
    if feature.status != "active":
        return f"{title}: {feature.status}"
    until = tz_format(feature.expires_at) if feature.expires_at else "бессрочно"
    return f"{title}: до {until}"
