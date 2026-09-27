"""Links to posts of the main channel inside texts the admins wrote, kept as symbols.

An admin's own text of the main post («✏️ Свой текст») or a category header may link to a post of the channel
the plain way: ``t.me/<channel>/73``. Such a link is kept as a symbol instead (``post:nav``, ``post:cat:5``,
``post:static:2``, as the import does), so when the navigation moves down every «#навигация» follows it. A
link to a post the bot no longer uses (the old navigation, a removed category) leads to the navigation.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Channel, ChannelPost, StaticPost
from app.domain.import_plan import symbolize
from app.domain.parse import ChannelInfo
from app.domain.richtext import Fragment
from app.services.channels import main_channel

LEFTOVERS = ("spare", "old_nav")  # posts of the channel the bot no longer shows anything in


async def post_keys(session: AsyncSession, channel: Channel) -> dict[int, str]:
    """Message id -> the symbol of what it shows (``nav``, ``cat:5``, ``static:2``)."""
    rows = (await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel.id))).scalars()
    keys: dict[int, str] = {}
    for row in rows:
        if not row.message_id:
            continue
        if row.kind == "nav":
            keys[row.message_id] = "nav"
        elif row.kind == "category":
            keys[row.message_id] = f"cat:{row.block_id}"
        elif row.kind == "static":
            keys[row.message_id] = f"static:{row.block_id}"
        elif row.kind in LEFTOVERS:
            keys.setdefault(row.message_id, "nav")
    return keys


def _info(channel: Channel) -> ChannelInfo:
    return ChannelInfo(chat_id=channel.chat_id, username=channel.username)


async def symbolize_draft(session: AsyncSession, fragment: Fragment) -> Fragment:
    """An admin's text before it is stored: its links to posts of the main channel become symbols."""
    channel = await main_channel(session)
    if channel is None:
        return fragment
    return symbolize(fragment, _info(channel), await post_keys(session, channel))


async def symbolize_stored(session: AsyncSession, channel: Channel) -> int:
    """The main post, other static posts and category headers with plain links to posts of ``channel``
    turned into symbols. Returns how many texts changed (the caller commits)."""
    keys = await post_keys(session, channel)
    if not keys:
        return 0
    info = _info(channel)
    changed = 0
    for post in (await session.execute(select(StaticPost))).scalars():
        before = Fragment.from_json(post.content)
        after = symbolize(before, info, keys)
        if after != before:
            post.content = after.to_json()
            changed += 1
    for category in (await session.execute(select(Category))).scalars():
        before = Fragment.from_json(category.header)
        after = symbolize(before, info, keys)
        if after != before:
            category.header = after.to_json()
            changed += 1
    if changed:
        await session.flush()
    return changed
