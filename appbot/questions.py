"""
ШАБЛОН АНКЕТЫ.

Чтобы изменить вопросы, правьте только список QUESTIONS ниже — больше ничего
менять не нужно. Количество вопросов не ограничено. Порядок в списке = порядок
вопросов в боте. Ответы привязаны к `key`, поэтому ключи должны быть уникальными
и не должны меняться у уже работающего бота (иначе старые ответы будут
отображаться под прежними подписями — это безопасно, но может сбить с толку).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class QType(str, Enum):
    TEXT = "text"          # текстовый ответ
    NUMBER = "number"      # целое число
    PHOTO = "photo"        # фото (как фото, не как файл)
    VIDEO = "video"        # видео
    DOCUMENT = "document"  # файл-документ
    MEDIA = "media"        # любые медиа: фото, видео, документы, GIF, аудио, голосовые, «кружки»
    ANY = "any"            # текст ИЛИ любой один файл (Telegram-сообщение/файл)


MEDIA_TYPES = {QType.PHOTO, QType.VIDEO, QType.DOCUMENT, QType.MEDIA}


@dataclass(frozen=True)
class Question:
    key: str                         # уникальный ключ ответа в БД
    text: str                        # что бот спрашивает у пользователя
    label: str                       # как ответ подписан в карточке заявки
    type: QType = QType.TEXT
    required: bool = True            # False — вопрос можно пропустить кнопкой
    multiple: bool = False           # для медиа-вопросов: несколько файлов + кнопка «Готово»
    min_files: int = 1               # минимум файлов (при multiple=True)
    max_files: int = 10              # максимум файлов (при multiple=True)
    max_length: int = 3500           # максимум символов текстового ответа
    min_value: int | None = None     # границы для NUMBER
    max_value: int | None = None
    pattern: str | None = None       # regex-проверка текстового ответа (re.search)
    pattern_error: str = "Ответ имеет неверный формат. Попробуйте ещё раз."


# Какой ответ использовать как заголовок заявки в списке («#125 — Nazar_Alfons»).
# None — показывать @username / имя пользователя Telegram.
APPLICATION_TITLE_KEY: str | None = "nick"

# Приветственное сообщение перед первым вопросом
APPLY_INTRO = (
    "📝 Начинаем заполнение заявки. Я буду задавать вопросы по одному — "
    "отвечайте на каждый отдельным сообщением.\n"
    "Отменить заполнение можно в любой момент командой /cancel."
)

QUESTIONS: list[Question] = [
    Question(
        key="nick",
        label="Ник",
        text="Ваш ник?",
        type=QType.TEXT,
        max_length=64,
    ),
    Question(
        key="balance",
        label="Счёт",
        text="Ваш счёт?",
        type=QType.TEXT,
        max_length=64,
    ),
    Question(
        key="screenshot",
        label="Скрин /c 60 с особняка",
        text="Скрин с /c 60 с особняка.",
        type=QType.PHOTO,
    ),
]

def validate_questions(questions: list[Question]) -> None:
    """Проверка шаблона при старте — чтобы ошибка в конфиге была видна сразу."""
    if not questions:
        raise ValueError("QUESTIONS пуст: в анкете должен быть хотя бы один вопрос")
    seen: set[str] = set()
    for q in questions:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", q.key):
            raise ValueError(f"Некорректный key {q.key!r}: допустимы латиница, цифры, _ и -, до 64 символов")
        if q.key in seen:
            raise ValueError(f"Дублирующийся key вопроса: {q.key!r}")
        seen.add(q.key)
        if not q.text.strip() or not q.label.strip():
            raise ValueError(f"У вопроса {q.key!r} пустой text или label")
        if q.multiple and q.type not in MEDIA_TYPES:
            raise ValueError(f"Вопрос {q.key!r}: multiple=True допустим только для медиа-типов")
        if q.multiple and not (1 <= q.min_files <= q.max_files):
            raise ValueError(f"Вопрос {q.key!r}: должно быть 1 <= min_files <= max_files")
        if q.pattern:
            re.compile(q.pattern)
        if q.min_value is not None and q.max_value is not None and q.min_value > q.max_value:
            raise ValueError(f"Вопрос {q.key!r}: min_value больше max_value")
