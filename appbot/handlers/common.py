"""Общие команды: /start, /help, /myapplications."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from appbot import permissions
from appbot.callbacks import MyListCb
from appbot.config import Settings
from appbot.db import repo
from appbot.texts import HELP_ADMIN, HELP_MANAGER, HELP_USER
from appbot.user_view import build_user_list
from appbot.utils import accessible, esc, safe_edit_text

router = Router(name="common")
router.message.filter(F.chat.type == "private")


async def _help_text(session: AsyncSession, user_id: int, settings: Settings) -> str:
    role = await permissions.get_role(session, user_id, settings.main_admin_id)
    text = HELP_USER
    if role is not None:
        text += HELP_ADMIN
    if permissions.can_manage_admins(role):
        text += HELP_MANAGER
    return text


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, settings: Settings) -> None:
    name = esc(message.from_user.first_name or "друг")
    await message.answer(
        f"👋 Здравствуйте, {name}!\n\nЭто бот для подачи заявок. Доступные команды:\n\n"
        + await _help_text(session, message.from_user.id, settings)
    )


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession, settings: Settings) -> None:
    await message.answer("<b>Команды бота:</b>\n\n" + await _help_text(session, message.from_user.id, settings))


@router.message(Command("myapplications"))
async def cmd_my_applications(message: Message, session: AsyncSession, settings: Settings) -> None:
    """Пользователь видит только СВОИ заявки. Сообщение запоминается и обновляется при смене статуса."""
    uid = message.from_user.id
    text, kb = await build_user_list(session, uid, settings)
    sent = await message.answer(text, reply_markup=kb)
    await repo.set_list_message(session, uid, sent.chat.id, sent.message_id)


@router.callback_query(MyListCb.filter(F.action == "refresh"))
async def cb_my_refresh(cb: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    msg = accessible(cb)
    if msg is None:
        await cb.answer("Сообщение устарело. Откройте список заново: /myapplications", show_alert=True)
        return
    text, kb = await build_user_list(session, cb.from_user.id, settings)
    await safe_edit_text(msg, text, kb)
    # Обновляемым становится то сообщение, с которым пользователь работает сейчас
    await repo.set_list_message(session, cb.from_user.id, msg.chat.id, msg.message_id)
    await cb.answer("Обновлено")
