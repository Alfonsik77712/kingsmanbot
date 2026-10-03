"""Создание async-движка и инициализация схемы."""
from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from appbot.db.models import Base

log = logging.getLogger(__name__)


def create_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, pool_pre_ping=True)

    if database_url.startswith("sqlite"):
        # SQLite: включаем внешние ключи, WAL (параллельные чтение/запись) и ожидание блокировок
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    # expire_on_commit=False — объекты остаются читаемыми после commit() без повторной загрузки
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Создаёт недостающие таблицы. Существующие данные не затрагиваются."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("База данных инициализирована")
