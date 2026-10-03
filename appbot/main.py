"""Точка входа: сборка зависимостей и запуск long polling."""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from appbot.commands import setup_bot_commands
from appbot.config import ConfigError, Settings, load_settings
from appbot.db import repo
from appbot.db.session import create_engine, create_session_factory, init_db
from appbot.handlers import admin, apply, common, fallback
from appbot.middlewares import DbSessionMiddleware
from appbot.questions import QUESTIONS, validate_questions

log = logging.getLogger("appbot")


def setup_logging(settings: Settings) -> None:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = RotatingFileHandler(
        settings.log_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.handlers = [console, file_handler]
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


async def on_error(event: ErrorEvent) -> bool:
    """Глобальный обработчик: логирует ошибку с трассировкой и вежливо сообщает пользователю."""
    log.error("Необработанная ошибка при обработке апдейта", exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
        elif update.message:
            await update.message.answer("⚠️ Произошла внутренняя ошибка. Попробуйте ещё раз чуть позже.")
    except Exception:  # noqa: BLE001 — уведомление об ошибке не должно само ронять бота
        log.debug("Не удалось уведомить пользователя об ошибке", exc_info=True)
    return True


async def main() -> None:
    settings = load_settings()
    setup_logging(settings)
    validate_questions(QUESTIONS)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    await init_db(engine)
    async with session_factory() as session:
        await repo.ensure_main_admin(session, settings.main_admin_id)
        await session.commit()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    # Хранилище FSM нужно только для короткого шага «ввод комментария администратора».
    # Состояние анкеты пользователей хранится в БД. Для кластера замените на RedisStorage.
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp.update.outer_middleware(DbSessionMiddleware(session_factory))
    # Порядок важен: админ-раздел → общие команды → анкета (ловит обычные сообщения) → прочее
    dp.include_routers(admin.router, common.router, apply.router, fallback.router)
    dp.errors.register(on_error)

    try:
        me = await bot.get_me()
        log.info("Бот запущен: @%s (id=%s), главный администратор: %s", me.username, me.id, settings.main_admin_id)
        await setup_bot_commands(bot, session_factory, settings)
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await engine.dispose()
        log.info("Бот остановлен")


def run() -> None:
    try:
        asyncio.run(main())
    except ConfigError as exc:
        raise SystemExit(f"Ошибка конфигурации: {exc}") from exc
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    run()
