"""Работа с медиафайлами: распознавание во входящих сообщениях и отправка админам."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from appbot.db.models import Answer, Media
from appbot.questions import QType, Question
from appbot.utils import esc

log = logging.getLogger(__name__)

PHOTO, VIDEO, DOCUMENT = "photo", "video", "document"
ANIMATION, AUDIO, VOICE, VIDEO_NOTE = "animation", "audio", "voice", "video_note"
ALL_KINDS = {PHOTO, VIDEO, DOCUMENT, ANIMATION, AUDIO, VOICE, VIDEO_NOTE}

KIND_NAMES = {
    PHOTO: "фото",
    VIDEO: "видео",
    DOCUMENT: "документ",
    ANIMATION: "GIF",
    AUDIO: "аудио",
    VOICE: "голосовое",
    VIDEO_NOTE: "кружок",
}

_ALLOWED = {
    QType.PHOTO: {PHOTO},
    QType.VIDEO: {VIDEO},
    QType.DOCUMENT: {DOCUMENT},
    QType.MEDIA: ALL_KINDS,
    QType.ANY: ALL_KINDS,
}


@dataclass(frozen=True)
class MediaInfo:
    kind: str
    file_id: str
    file_unique_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


def extract_media(message: Message) -> MediaInfo | None:
    """Достаёт файл из сообщения. None — в сообщении нет поддерживаемого медиа."""
    if message.photo:
        p = message.photo[-1]  # самый большой размер
        return MediaInfo(PHOTO, p.file_id, p.file_unique_id, None, None, p.file_size)
    # animation проверяем раньше document: у GIF заполнены оба поля
    if message.animation:
        a = message.animation
        return MediaInfo(ANIMATION, a.file_id, a.file_unique_id, a.file_name, a.mime_type, a.file_size)
    if message.video:
        v = message.video
        return MediaInfo(VIDEO, v.file_id, v.file_unique_id, v.file_name, v.mime_type, v.file_size)
    if message.document:
        d = message.document
        return MediaInfo(DOCUMENT, d.file_id, d.file_unique_id, d.file_name, d.mime_type, d.file_size)
    if message.audio:
        a = message.audio
        return MediaInfo(AUDIO, a.file_id, a.file_unique_id, a.file_name, a.mime_type, a.file_size)
    if message.voice:
        v = message.voice
        return MediaInfo(VOICE, v.file_id, v.file_unique_id, None, v.mime_type, v.file_size)
    if message.video_note:
        v = message.video_note
        return MediaInfo(VIDEO_NOTE, v.file_id, v.file_unique_id, None, None, v.file_size)
    return None


def allowed_kinds(question: Question) -> set[str]:
    return _ALLOWED.get(question.type, set())


def group_media_by_answer(media: list[Media]) -> dict[int, list[Media]]:
    grouped: dict[int, list[Media]] = defaultdict(list)
    for item in media:
        grouped[item.answer_id].append(item)
    return grouped


# ------------------------------------------------------------------ отправка админам
_GROUP_LIMIT = 10  # максимум элементов в альбоме Telegram


async def _call(coro_factory, what: str):
    """Вызов Telegram API с одним повтором при flood-лимите. Возвращает True/False."""
    for _ in range(2):
        try:
            await coro_factory()
            return True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
        except TelegramAPIError:
            log.exception("Не удалось отправить медиа: %s", what)
            return False
    return False


async def _send_single(bot: Bot, chat_id: int, m: Media, caption: str | None):
    kind = m.kind
    if kind == PHOTO:
        return await bot.send_photo(chat_id, m.file_id, caption=caption)
    if kind == VIDEO:
        return await bot.send_video(chat_id, m.file_id, caption=caption)
    if kind == ANIMATION:
        return await bot.send_animation(chat_id, m.file_id, caption=caption)
    if kind == AUDIO:
        return await bot.send_audio(chat_id, m.file_id, caption=caption)
    if kind == VOICE:
        return await bot.send_voice(chat_id, m.file_id, caption=caption)
    if kind == VIDEO_NOTE:
        return await bot.send_video_note(chat_id, m.file_id)  # у «кружков» нет подписи
    return await bot.send_document(chat_id, m.file_id, caption=caption)


def _input_media(m: Media, caption: str | None):
    if m.kind == PHOTO:
        return InputMediaPhoto(media=m.file_id, caption=caption)
    if m.kind == VIDEO:
        return InputMediaVideo(media=m.file_id, caption=caption)
    if m.kind == AUDIO:
        return InputMediaAudio(media=m.file_id, caption=caption)
    return InputMediaDocument(media=m.file_id, caption=caption)


async def _send_chunk(bot: Bot, chat_id: int, chunk: list[Media], caption: str | None) -> None:
    if len(chunk) == 1:
        ok = await _call(lambda: _send_single(bot, chat_id, chunk[0], caption), f"{chunk[0].kind} id={chunk[0].id}")
    else:
        items = [_input_media(m, caption if i == 0 else None) for i, m in enumerate(chunk)]
        ok = await _call(lambda: bot.send_media_group(chat_id, items), f"альбом из {len(chunk)} файлов")
    if not ok:
        await _call(
            lambda: bot.send_message(chat_id, "⚠️ Не удалось отправить часть файлов (см. логи бота)."),
            "уведомление об ошибке",
        )


async def send_application_media(bot: Bot, chat_id: int, answers: list[Answer], media: list[Media]) -> None:
    """Отправляет все файлы заявки: фото/видео, документы и аудио — альбомами по ≤10 штук.

    GIF, голосовые и «кружки» Telegram не умеет объединять в альбомы — они идут по одному.
    """
    by_answer = group_media_by_answer(media)
    for answer in answers:
        items = by_answer.get(answer.id)
        if not items:
            continue
        caption = f"📎 {esc(answer.question_label)}"[:1000]
        groups = [
            [m for m in items if m.kind in (PHOTO, VIDEO)],
            [m for m in items if m.kind == DOCUMENT],
            [m for m in items if m.kind == AUDIO],
            [m for m in items if m.kind in (ANIMATION, VOICE, VIDEO_NOTE)],
        ]
        caption_used = False
        for gi, group in enumerate(groups):
            if not group:
                continue
            # Последняя группа — одиночные сообщения
            size = 1 if gi == 3 else _GROUP_LIMIT
            for i in range(0, len(group), size):
                chunk = group[i : i + size]
                await _send_chunk(bot, chat_id, chunk, None if caption_used else caption)
                caption_used = True
                await asyncio.sleep(0.05)
