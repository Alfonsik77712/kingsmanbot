"""Меню команд Telegram: свой набор для обычных пользователей и для администраторов."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat

from appbot.config import Settings
from appbot.db import repo
from appbot.permissions import Role

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Запуск бота"),
    BotCommand(command="apply", description="Подать заявку"),
    BotCommand(command="cancel", description="Отменить заполнение"),
    BotCommand(command="myapplications", description="Мои заявки"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="applications", description="Список заявок"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="admins", description="Список администраторов"),
]
MANAGER_COMMANDS = ADMIN_COMMANDS + [
    BotCommand(command="addadmin", description="Добавить администратора"),
    BotCommand(command="removeadmin", description="Удалить администратора"),
]


def commands_for(role: Role) -> list[BotCommand]:
    return ADMIN_COMMANDS if role == Role.ADMIN else MANAGER_COMMANDS


async def grant_commands(bot: Bot, user_id: int, role: Role) -> None:
    try:
        await bot.set_my_commands(commands_for(role), scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError as exc:
        # Например, пользователь ещё не запускал бота — меню появится позже
        log.info("Не удалось выставить меню команд для %s: %s", user_id, exc)


async def revoke_commands(bot: Bot, user_id: int) -> None:
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError as exc:
        log.info("Не удалось сбросить меню команд для %s: %s", user_id, exc)


async def setup_bot_commands(bot: Bot, session_factory, settings: Settings) -> None:
    try:
        await bot.set_my_commands(USER_COMMANDS)
    except TelegramAPIError:
        log.exception("Не удалось установить меню команд по умолчанию")
    async with session_factory() as session:
        admins = await repo.list_admins(session)
    await grant_commands(bot, settings.main_admin_id, Role.MAIN)
    for admin, _user in admins:
        if admin.user_id == settings.main_admin_id:
            continue
        await grant_commands(bot, admin.user_id, Role.MANAGER if admin.is_manager else Role.ADMIN)
