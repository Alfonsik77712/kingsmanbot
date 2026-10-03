"""Загрузка настроек из переменных окружения / файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# Корень проекта — папка, в которой лежат .env, data/ и logs/
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Ошибка конфигурации: бот не может стартовать."""


@dataclass(frozen=True)
class Settings:
    bot_token: str
    main_admin_id: int
    database_url: str
    timezone: ZoneInfo
    page_size: int
    notify_admins_on_new: bool
    max_pending_per_user: int
    log_level: str
    log_dir: Path


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _get_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} должно быть не меньше {minimum}")
    return value


def _normalize_database_url(url: str) -> str:
    """Приводит URL к async-драйверам и создаёт папку для SQLite-файла."""
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    prefix = "sqlite+aiosqlite:///"
    if url.startswith(prefix):
        raw_path = url[len(prefix):]
        if raw_path and raw_path != ":memory:":
            path = Path(raw_path)
            if not path.is_absolute():
                path = BASE_DIR / path
            path.parent.mkdir(parents=True, exist_ok=True)
            url = prefix + path.as_posix()
    return url


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("Не задан BOT_TOKEN (см. файл .env.example)")

    raw_admin = os.getenv("MAIN_ADMIN_ID", "").strip()
    if not raw_admin.lstrip("-").isdigit() or int(raw_admin) <= 0:
        raise ConfigError("MAIN_ADMIN_ID должен быть положительным числом — Telegram ID главного администратора")

    tz_name = os.getenv("TIMEZONE", "UTC").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Неизвестный часовой пояс TIMEZONE={tz_name!r}") from exc

    default_db = "sqlite+aiosqlite:///data/bot.db"
    database_url = _normalize_database_url(os.getenv("DATABASE_URL", "").strip() or default_db)

    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        bot_token=token,
        main_admin_id=int(raw_admin),
        database_url=database_url,
        timezone=tz,
        page_size=_get_int("PAGE_SIZE", 10, minimum=1),
        notify_admins_on_new=_get_bool("NOTIFY_ADMINS_ON_NEW", True),
        max_pending_per_user=_get_int("MAX_PENDING_PER_USER", 0, minimum=0),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        log_dir=log_dir,
    )
