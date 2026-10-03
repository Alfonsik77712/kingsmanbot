"""Модели базы данных (SQLAlchemy 2.0, async).

Даты хранятся как «наивное» время в UTC; в локальный часовой пояс они
переводятся только при отображении (см. utils.fmt_dt).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Status:
    """Статусы заявки (хранятся в БД строками)."""

    DRAFT = "draft"          # заполняется пользователем
    PENDING = "pending"      # на рассмотрении
    APPROVED = "approved"    # одобрена
    REJECTED = "rejected"    # отклонена
    CANCELLED = "cancelled"  # заполнение отменено пользователем


# Статусы, которые видят администраторы (черновики и отменённые скрыты)
VISIBLE_STATUSES = (Status.PENDING, Status.APPROVED, Status.REJECTED)

STATUS_TITLES = {
    Status.DRAFT: "✏️ Заполняется",
    Status.PENDING: "🟡 На рассмотрении",
    Status.APPROVED: "🟢 Одобрена",
    Status.REJECTED: "🔴 Отклонена",
    Status.CANCELLED: "⚪ Отменена",
}
STATUS_EMOJI = {
    Status.DRAFT: "✏️",
    Status.PENDING: "🟡",
    Status.APPROVED: "🟢",
    Status.REJECTED: "🔴",
    Status.CANCELLED: "⚪",
}


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(256))
    last_name: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p) or ""


class Admin(Base):
    """Администраторы. Главный администратор задаётся в конфиге, но тоже хранится здесь."""

    __tablename__ = "admins"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # True — может добавлять/удалять обычных администраторов (право выдаёт только главный админ)
    is_manager: Mapped[bool] = mapped_column(Boolean, default=False)
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        # Не более одного черновика на пользователя — защита от параллельных заявок
        # (работает и при нескольких процессах бота).
        Index(
            "uq_applications_one_draft_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("status = 'draft'"),
            postgresql_where=text("status = 'draft'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    # Снимок данных пользователя на момент подачи
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default=Status.DRAFT, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    processed_by: Mapped[int | None] = mapped_column(BigInteger)  # Telegram ID администратора
    admin_comment: Mapped[str | None] = mapped_column(Text)


class Answer(Base):
    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("application_id", "question_key", name="uq_answer_app_question"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    question_key: Mapped[str] = mapped_column(String(64))
    # Снимки текста вопроса и подписи: изменение шаблона не ломает старые заявки
    question_label: Mapped[str] = mapped_column(String(256))
    question_text: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)
    text_value: Mapped[str | None] = mapped_column(Text)
    # False — ответ начат, но не завершён (например, набираются файлы до «Готово»)
    is_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Media(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    answer_id: Mapped[int] = mapped_column(Integer, ForeignKey("answers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # photo/video/document/animation/audio/voice/video_note
    file_id: Mapped[str] = mapped_column(String(512))  # Telegram file_id — для повторной отправки
    file_unique_id: Mapped[str | None] = mapped_column(String(128))
    file_name: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    caption: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StatusHistory(Base):
    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    old_status: Mapped[str | None] = mapped_column(String(16))
    new_status: Mapped[str] = mapped_column(String(16))
    admin_id: Mapped[int | None] = mapped_column(BigInteger)  # None — действие пользователя
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserListMessage(Base):
    """Последнее сообщение «Ваши заявки» у пользователя — его бот обновляет при смене статуса."""

    __tablename__ = "user_list_messages"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
