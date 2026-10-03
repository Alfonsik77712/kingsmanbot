"""Middleware: сессия БД, последовательная обработка апдейтов одного пользователя, защита админ-раздела."""
from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from appbot import permissions
from appbot.db import repo

log = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    """Для каждого апдейта открывает сессию БД и регистрирует/обновляет пользователя.

    Апдейты одного пользователя обрабатываются СТРОГО ПО ОЧЕРЕДИ (через asyncio.Lock):
    это исключает гонки при двойных нажатиях, альбомах из нескольких файлов и
    параллельных /apply. Сессия коммитится после успешного хендлера и откатывается при ошибке.
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        # Слабые ссылки: неиспользуемые блокировки удаляются сами, память не растёт
        self._locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        lock = self._locks.get(tg_user.id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[tg_user.id] = lock

        async with lock:
            async with self._session_factory() as session:
                data["session"] = session
                data["db_user"] = await repo.upsert_user(session, tg_user)
                await session.commit()
                try:
                    result = await handler(event, data)
                    await session.commit()
                    return result
                except Exception:
                    await session.rollback()
                    raise


class AdminOnlyMiddleware(BaseMiddleware):
    """Пропускает к админским хендлерам только администраторов. Проверка идёт в БД при КАЖДОМ обращении."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data["event_from_user"]
        settings = data["settings"]
        role = await permissions.get_role(data["session"], tg_user.id, settings.main_admin_id)
        if role is None:
            log.warning("Отказ в доступе: пользователь %s (@%s) обратился к админ-функции", tg_user.id, tg_user.username)
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Недостаточно прав.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("⛔ У вас недостаточно прав для этой команды.")
            return None
        data["role"] = role
        return await handler(event, data)
