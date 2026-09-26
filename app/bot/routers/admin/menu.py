"""Admin: the video / GIF / picture shown above the bot's main menu."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.flows.start import menu_media, send_menu, show_media_menu, show_screen
from app.bot.routers.admin.inputs import input_handler
from app.bot.routers.admin.panel import back_home
from app.bot.states import AdminInput
from app.context import AppContext
from app.services.audit import audit
from app.services.media import DOWNLOAD_LIMIT, media_of, store_file
from app.services.settings import MenuMedia, save_settings

router = Router(name="admin_menu")
router.callback_query.filter(RoleFilter("admin"))

KIND_NAMES = {"video": "видео", "animation": "GIF", "photo": "картинка"}
BIG_FILE = (
    "⚠️ Файл больше 20 МБ: бот не может сохранить его копию. В резервные копии он не попадёт, а при "
    "смене бота заставку нужно будет загрузить заново. Лучше сжать видео до 20 МБ."
)


async def _screen(session: AsyncSession, *, waiting: bool) -> tuple[str, Any]:
    media = await menu_media(session)
    lines = [
        "🎬 <b>Заставка главного меню</b>",
        "",
        "Видео, GIF или картинка над главным меню бота. Текст меню становится подписью к ней.",
        "",
    ]
    builder = InlineKeyboardBuilder()
    if media is None:
        lines.append("Сейчас заставки нет — меню показывается текстом.")
    else:
        size = f", {media.size / 1024 / 1024:.1f} МБ" if media.size else ""
        copy = "копия сохранена" if media.local_path else "без копии у бота"
        lines.append(f"Сейчас: {KIND_NAMES.get(media.kind, media.kind)}{size}, {copy}.")
        builder.button(text="👁 Показать меню", callback_data="a:menu:show")
        builder.button(text="🗑 Убрать заставку", callback_data="a:menu:del")
    if waiting:
        lines += ["", "Чтобы поставить или заменить, пришлите видео, GIF или картинку следующим сообщением."]
    else:
        builder.button(text="🔄 Заменить", callback_data="a:menu")
    builder.adjust(1)
    return "\n".join(lines), back_home(builder, target="a:tpl")


@router.callback_query(F.data == "a:menu")
async def on_screen(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.set_state(AdminInput.waiting)
    await state.set_data({"purpose": "menu_media", "back": "a:tpl"})
    await call.answer()
    text, markup = await _screen(session, waiting=True)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data == "a:menu:show")
async def on_show(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    await call.answer()
    assert call.message is not None
    await send_menu(call.message.chat.id, {**data, "session": session})


@router.callback_query(F.data == "a:menu:del")
async def on_delete(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await save_settings(session, MenuMedia())
    await audit(session, data["user"].id, "menu.media_remove", "settings", MenuMedia.KEY)
    await session.commit()
    await call.answer("Заставка убрана")
    text, markup = await _screen(session, waiting=False)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@input_handler("menu_media")
async def input_menu_media(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    ctx: AppContext = data["ctx"]
    found = media_of(message)
    if found is None:
        await message.answer("Нужно видео, GIF или картинка. Пришлите файл или нажмите «⬅️ Назад».")
        return False
    file_id, unique_id, kind, mime, size, as_file = found
    if as_file and size and size > DOWNLOAD_LIMIT:
        await message.answer(
            "Файл больше 20 МБ пришёл как документ — такие файлы Telegram боту не отдаёт. Отправьте его "
            "как видео (без галочки «отправить как файл») или сожмите до 20 МБ."
        )
        return False
    record = await store_file(ctx, session, file_id, unique_id, kind=kind, mime=mime, size=size)
    if as_file:
        if not record.local_path:
            await message.answer("Не удалось скачать файл. Отправьте его как видео, а не как файл.")
            return False
        record.file_id = None  # a document's file_id cannot be sent as a video: the local copy is uploaded
    await save_settings(session, MenuMedia(media_id=record.id, kind=kind))
    await audit(session, data["user"].id, "menu.media", "media", record.id, {"kind": kind})
    await session.commit()
    if not await show_media_menu(message.chat.id, data, record):
        await save_settings(session, MenuMedia())
        await session.commit()
        await message.answer(
            "⚠️ Telegram не принял этот файл как заставку. Попробуйте другой: mp4 до 50 МБ, GIF или картинку."
        )
        return False
    note = "✅ Заставка сохранена — так меню видят пользователи (сообщение выше)."
    if not record.local_path:
        note += "\n\n" + BIG_FILE
    text, markup = await _screen(session, waiting=False)
    await message.answer(f"{note}\n\n{text}", reply_markup=markup)
    return True
