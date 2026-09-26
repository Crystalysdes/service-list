"""Building render views and link contexts from the database."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Channel, ChannelPost, MediaFile, Service, StaticPost
from app.domain.fonts import glyphs_from_json
from app.domain.render import (
    CategoryView,
    ItemView,
    Limits,
    NavItem,
    RenderTemplates,
    render_category,
    render_nav,
    render_static,
)
from app.domain.richtext import Fragment
from app.domain.symbols import LinkContext, channel_post_base, channel_url
from app.services.channels import INACTIVE_STATUSES
from app.services.settings import Limits as LimitSettings
from app.services.settings import Runtime, Templates, get_settings

VISIBLE_STATUSES = ("active",)


def active_feature(service: Service, kind: str) -> Any:
    for feature in service.features:
        if feature.kind == kind and feature.status == "active":
            return feature
    return None


def item_from_service(service: Service) -> ItemView:
    emoji_feature = active_feature(service, "emoji")
    font_feature = active_feature(service, "font")
    emoji = None
    if emoji_feature is not None and emoji_feature.params.get("emoji_id"):
        emoji = (str(emoji_feature.params["emoji_id"]), str(emoji_feature.params.get("alt") or "⭐"))
    glyphs = glyphs_from_json(font_feature.params.get("glyphs")) if font_feature is not None else None
    return ItemView(
        name=service.name,
        url=service.url,
        emoji=emoji,
        glyphs=glyphs or None,
        raw=Fragment.from_json(service.raw_fragment) if service.raw_fragment else None,
        note=service.url_kind == "note",
        name_styles=tuple((service.extra or {}).get("name_styles") or ()),
        service_id=service.id,
    )


def order_services(services: list[Service]) -> list[Service]:
    def key(service: Service) -> tuple[int, int, int, int]:
        top = active_feature(service, "top")
        if top is not None and top.top_position:
            return (0, int(top.top_position), 0, service.id)
        return (1, 0, service.position, service.id)

    return sorted(services, key=key)


async def category_services(session: AsyncSession, category_id: int) -> list[Service]:
    rows = await session.execute(
        select(Service).where(Service.category_id == category_id, Service.status.in_(VISIBLE_STATUSES))
    )
    return order_services(list(rows.scalars().unique()))


async def category_view(
    session: AsyncSession, category: Category, extra: list[ItemView] | None = None
) -> CategoryView:
    services = await category_services(session, category.id)
    items = [item_from_service(s) for s in services]
    if extra:
        items.extend(extra)
    return CategoryView(
        id=category.id, slug=category.slug, header=Fragment.from_json(category.header), items=items
    )


async def templates(session: AsyncSession) -> RenderTemplates:
    return RenderTemplates.from_settings(await get_settings(session, Templates))


async def limits(session: AsyncSession) -> Limits:
    settings = await get_settings(session, LimitSettings)
    runtime = await get_settings(session, Runtime)
    emoji_cap = settings.max_custom_emoji_per_post
    if runtime.custom_emoji_cap:
        emoji_cap = min(emoji_cap, runtime.custom_emoji_cap)
    entity_cap = settings.max_user_entities
    if runtime.entity_cap:
        entity_cap = min(entity_cap, runtime.entity_cap)
    return Limits(max_user_entities=entity_cap, max_custom_emoji=emoji_cap)


async def channel_urls(session: AsyncSession) -> dict[str, str]:
    urls: dict[str, str] = {}
    rows = await session.execute(
        select(Channel)
        .where(Channel.role.in_(("main", "scam")), Channel.status.not_in(INACTIVE_STATUSES))
        .order_by(Channel.id)
    )
    for channel in rows.scalars():
        url = channel_url(channel.chat_id, channel.username, channel.invite_link)
        if url and channel.role not in urls:
            urls[channel.role] = url
    return urls


async def link_context(session: AsyncSession, channel: Channel, bot_username: str | None) -> LinkContext:
    rows = await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel.id))
    posts: dict[str, int] = {}
    for row in rows.scalars():
        if not row.message_id:
            continue
        if row.kind == "nav":
            posts["nav"] = row.message_id
        elif row.kind == "category":
            posts[f"cat:{row.block_id}"] = row.message_id
        elif row.kind == "static":
            posts[f"static:{row.block_id}"] = row.message_id
    return LinkContext(
        bot_username=bot_username,
        post_base=channel_post_base(channel.chat_id, channel.username),
        posts=posts,
        channels=await channel_urls(session),
    )


@dataclass
class RenderedBlock:
    kind: str
    block_id: int
    fragment: Fragment
    media: MediaFile | None = None
    media_kind: str | None = None
    link_preview: bool = False

    @property
    def is_caption(self) -> bool:
        return self.media is not None

    def content_hash(self) -> str:
        media_key = self.media.sha256 or self.media.file_unique_id if self.media is not None else None
        return self.fragment.content_hash(media_key, self.link_preview)


async def nav_items(session: AsyncSession) -> list[NavItem]:
    entries: list[tuple[int, NavItem]] = []
    for category in (await session.execute(select(Category).where(Category.is_visible))).scalars():
        if category.nav_label:
            entries.append((category.nav_order, NavItem(category.nav_label, f"cat:{category.id}")))
    for post in (await session.execute(select(StaticPost))).scalars():
        if post.nav_label:
            entries.append((post.nav_order, NavItem(post.nav_label, f"static:{post.id}")))
    entries.sort(key=lambda e: e[0])
    return [item for _, item in entries]


async def render_block(
    session: AsyncSession, kind: str, block_id: int, ctx: LinkContext, tpl: RenderTemplates
) -> RenderedBlock | None:
    if kind == "category":
        category = await session.get(Category, block_id)
        if category is None:
            return None
        view = await category_view(session, category)
        return RenderedBlock(kind, block_id, render_category(view, tpl, ctx))
    if kind == "static":
        post = await session.get(StaticPost, block_id)
        if post is None:
            return None
        media = await session.get(MediaFile, post.media_id) if post.media_id else None
        return RenderedBlock(
            kind,
            block_id,
            render_static(Fragment.from_json(post.content), ctx),
            media=media,
            media_kind=post.media_kind,
            link_preview=post.link_preview,
        )
    if kind == "nav":
        return RenderedBlock(kind, 0, render_nav(await nav_items(session), tpl, ctx))
    if kind == "spare":
        return RenderedBlock(kind, block_id, Fragment.plain("⠀"))
    return None


async def desired_blocks(session: AsyncSession) -> list[tuple[str, int]]:
    """Blocks of a main/mirror channel in their channel order; the nav is always last."""
    blocks: list[tuple[int, str, int]] = []
    for post in (await session.execute(select(StaticPost))).scalars():
        blocks.append((post.post_order, "static", post.id))
    for category in (await session.execute(select(Category).where(Category.is_visible))).scalars():
        blocks.append((category.post_order, "category", category.id))
    blocks.sort(key=lambda b: (b[0], b[1], b[2]))
    return [(kind, block_id) for _, kind, block_id in blocks] + [("nav", 0)]
