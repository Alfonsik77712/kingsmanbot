"""Мелкие утилиты: экранирование, даты, безопасная отправка сообщений."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone, tzinfo

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import CallbackQuery, Message

from appbot.db.models import utcnow

log = logging.getLogger(__name__)

# Лимит Telegram — 4096 символов; оставляем запас на HTML-теги
MESSAGE_LIMIT = 4000


def esc(value) -> str:
    """Экранирование пользовательских данных для parse_mode=HTML."""
    return html.escape("" if value is None else str(value), quote=False)


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def fmt_dt(dt: datetime | None, tz: tzinfo) -> str:
    if dt is None:
        return "—"
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%d.%m.%Y %H:%M")


def day_start_utc(tz: tzinfo) -> datetime:
    """Начало текущих суток в часовом поясе бота, в «наивном» UTC (как хранится в БД)."""
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def week_start_utc() -> datetime:
    """«За неделю» = последние 7 суток."""
    return utcnow() - timedelta(days=7)


def not_command(message: Message) -> bool:
    """Фильтр: сообщение не является командой."""
    return not (message.text and message.text.startswith("/"))


def accessible(cb: CallbackQuery) -> Message | None:
    """Сообщение с кнопкой (None, если оно слишком старое/недоступно)."""
    return cb.message if isinstance(cb.message, Message) else None


def _safe_cut(line: str, limit: int) -> int:
    cut = limit
    amp = line.rfind("&", 0, cut)
    # Не режем HTML-сущность (&amp; &lt; &gt;) посередине
    if amp != -1 and ";" not in line[amp:cut] and cut - amp <= 6:
        cut = amp
    return cut or limit


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Делит длинный HTML-текст на части по границам строк (длинные строки — жёстко)."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            while len(line) > limit:
                cut = _safe_cut(line, limit)
                chunks.append(line[:cut])
                line = line[cut:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()] or [""]


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> Message | None:
    """Отправка без падений: обрабатывает блокировку бота, flood-лимиты и ошибки API."""
    for _ in range(2):
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except TelegramRetryAfter as exc:
            log.warning("Flood control: ждём %s c (chat %s)", exc.retry_after, chat_id)
            await asyncio.sleep(exc.retry_after + 0.5)
        except TelegramForbiddenError:
            log.info("Не удалось отправить сообщение: чат %s недоступен (бот заблокирован?)", chat_id)
            return None
        except TelegramAPIError:
            log.exception("Ошибка Telegram API при отправке сообщения в чат %s", chat_id)
            return None
    return None


async def safe_edit_text(message: Message, text: str, reply_markup=None) -> bool:
    """Редактирует сообщение. False — текст не изменился («message is not modified»)."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return False
        log.warning("Не удалось отредактировать сообщение (%s) — отправляем новое", exc)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 0.5)
    except TelegramAPIError:
        log.exception("Ошибка Telegram API при редактировании сообщения")
    try:
        await message.answer(text, reply_markup=reply_markup)
    except TelegramAPIError:
        log.exception("Не удалось отправить замену сообщения")
    return True


async def safe_edit_by_id(bot: Bot, chat_id: int, message_id: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            log.warning("Не удалось отредактировать сообщение %s: %s", message_id, exc)
        return False
    except TelegramAPIError:
        log.exception("Ошибка Telegram API при редактировании сообщения %s", message_id)
        return False


async def safe_clear_markup(message: Message) -> None:
    """Убирает inline-кнопки у сообщения (ошибки игнорируются)."""
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass


# Лимит подписи к фото в Telegram
CAPTION_LIMIT = 1024


async def show_screen(
    bot: Bot,
    chat_id: int,
    message_id: int,
    cur_photo: bool,
    text: str,
    reply_markup=None,
    photo: str | None = None,
) -> int | None:
    """Показывает «экран» на месте сообщения message_id. Возвращает id сообщения с экраном
    (оно может измениться) либо None, если ничего не изменилось.

    cur_photo — текущее сообщение фото-сообщение; photo — file_id фото для нового экрана.
    Telegram не умеет превращать текстовое сообщение в фото и обратно, поэтому при смене
    типа старое сообщение удаляется, а новое отправляется.
    """
    want_photo = photo is not None
    try:
        if cur_photo and want_photo:
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, reply_markup=reply_markup)
            return message_id
        if not cur_photo and not want_photo:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
            return message_id
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return None
        log.warning("Не удалось отредактировать сообщение %s (%s) — отправляем новое", message_id, exc)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 0.5)
    except TelegramAPIError:
        log.exception("Ошибка Telegram API при редактировании сообщения %s", message_id)

    # Смена типа сообщения (или неудачная правка): удаляем старое, отправляем новое
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        log.debug("Не удалось удалить сообщение %s", message_id, exc_info=True)
    try:
        if want_photo:
            sent = await bot.send_photo(chat_id, photo, caption=text, reply_markup=reply_markup)
        else:
            sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
        return sent.message_id
    except TelegramAPIError:
        log.exception("Не удалось отправить экран в чат %s", chat_id)
        return None
