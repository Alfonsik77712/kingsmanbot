"""Все запросы к БД собраны здесь — хендлеры не пишут SQL напрямую."""
from __future__ import annotations

from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from appbot.db.models import (
    VISIBLE_STATUSES,
    Admin,
    Answer,
    Application,
    Media,
    Status,
    StatusHistory,
    User,
    UserListMessage,
    utcnow,
)
from appbot.questions import Question


# --------------------------------------------------------------------------- users
async def upsert_user(session: AsyncSession, tg_user: TgUser) -> User:
    user = await session.get(User, tg_user.id)
    now = utcnow()
    if user is None:
        user = User(
            telegram_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            created_at=now,
            last_seen_at=now,
        )
        session.add(user)
    else:
        user.username = tg_user.username
        user.first_name = tg_user.first_name
        user.last_name = tg_user.last_name
        user.last_seen_at = now
    await session.flush()
    return user


# -------------------------------------------------------------------------- admins
async def get_admin(session: AsyncSession, user_id: int) -> Admin | None:
    return await session.get(Admin, user_id)


async def list_admins(session: AsyncSession) -> list[tuple[Admin, User | None]]:
    result = await session.execute(
        select(Admin, User).outerjoin(User, User.telegram_id == Admin.user_id).order_by(Admin.added_at, Admin.user_id)
    )
    return [(admin, user) for admin, user in result.all()]


async def list_admin_ids(session: AsyncSession) -> list[int]:
    result = await session.execute(select(Admin.user_id))
    return list(result.scalars().all())


async def ensure_main_admin(session: AsyncSession, main_admin_id: int) -> None:
    if await session.get(Admin, main_admin_id) is None:
        session.add(Admin(user_id=main_admin_id, is_manager=True, added_by=None))
        await session.flush()


async def add_admin(session: AsyncSession, user_id: int, added_by: int, is_manager: bool) -> Admin:
    admin = Admin(user_id=user_id, is_manager=is_manager, added_by=added_by)
    session.add(admin)
    await session.flush()
    return admin


async def remove_admin(session: AsyncSession, user_id: int) -> bool:
    admin = await session.get(Admin, user_id)
    if admin is None:
        return False
    await session.delete(admin)
    await session.flush()
    return True


# ---------------------------------------------------------------------- applications
async def get_draft(session: AsyncSession, user_id: int) -> Application | None:
    result = await session.execute(
        select(Application).where(Application.user_id == user_id, Application.status == Status.DRAFT)
    )
    return result.scalar_one_or_none()


async def create_draft(session: AsyncSession, user: User) -> Application | None:
    """Создаёт черновик. None — у пользователя уже есть черновик (сработал уникальный индекс)."""
    app = Application(
        user_id=user.telegram_id,
        username=user.username,
        full_name=user.full_name or None,
        status=Status.DRAFT,
    )
    session.add(app)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return None
    return app


async def count_user_applications(session: AsyncSession, user_id: int, status: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(Application).where(Application.user_id == user_id, Application.status == status)
    )
    return int(result.scalar_one())


async def get_application(session: AsyncSession, app_id: int) -> Application | None:
    return await session.get(Application, app_id)


async def list_user_applications(session: AsyncSession, user_id: int, limit: int = 20) -> list[Application]:
    result = await session.execute(
        select(Application)
        .where(Application.user_id == user_id, Application.status.in_(VISIBLE_STATUSES))
        .order_by(Application.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


def _visible_filter(status: str | None):
    if status is None:
        return Application.status.in_(VISIBLE_STATUSES)
    return Application.status == status


async def count_applications(session: AsyncSession, status: str | None) -> int:
    result = await session.execute(select(func.count()).select_from(Application).where(_visible_filter(status)))
    return int(result.scalar_one())


async def list_applications(session: AsyncSession, status: str | None, page: int, page_size: int) -> list[Application]:
    result = await session.execute(
        select(Application)
        .where(_visible_filter(status))
        .order_by(Application.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    return list(result.scalars().all())


async def get_titles(session: AsyncSession, app_ids: list[int], key: str | None) -> dict[int, str]:
    """Значение ответа-«заголовка» для списка заявок."""
    if not app_ids or not key:
        return {}
    result = await session.execute(
        select(Answer.application_id, Answer.text_value).where(
            Answer.application_id.in_(app_ids), Answer.question_key == key
        )
    )
    return {app_id: value for app_id, value in result.all() if value}


async def submit_application(session: AsyncSession, app: Application, user: User) -> Application:
    now = utcnow()
    app.status = Status.PENDING
    app.submitted_at = now
    app.status_changed_at = now
    app.username = user.username
    app.full_name = user.full_name or None
    session.add(
        StatusHistory(
            application_id=app.id, old_status=Status.DRAFT, new_status=Status.PENDING, admin_id=None, created_at=now
        )
    )
    await session.flush()
    return app


async def cancel_draft(session: AsyncSession, user_id: int) -> Application | None:
    app = await get_draft(session, user_id)
    if app is None:
        return None
    app.status = Status.CANCELLED
    session.add(
        StatusHistory(application_id=app.id, old_status=Status.DRAFT, new_status=Status.CANCELLED, admin_id=None)
    )
    await session.flush()
    return app


async def change_status(
    session: AsyncSession, app_id: int, new_status: str, admin_id: int, comment: str | None
) -> tuple[Application, str] | None:
    """Меняет статус и пишет историю. Возвращает (заявка, старый_статус) либо None."""
    app = await session.get(Application, app_id, with_for_update=True)
    if app is None or app.status not in VISIBLE_STATUSES:
        return None
    old = app.status
    now = utcnow()
    app.status = new_status
    app.processed_by = admin_id
    app.status_changed_at = now
    app.admin_comment = comment
    session.add(
        StatusHistory(
            application_id=app.id,
            old_status=old,
            new_status=new_status,
            admin_id=admin_id,
            comment=comment,
            created_at=now,
        )
    )
    await session.flush()
    return app, old


async def get_history(session: AsyncSession, app_id: int) -> list[StatusHistory]:
    result = await session.execute(
        select(StatusHistory).where(StatusHistory.application_id == app_id).order_by(StatusHistory.id)
    )
    return list(result.scalars().all())


async def get_stats(session: AsyncSession, day_start, week_start) -> dict[str, int]:
    rows = await session.execute(
        select(Application.status, func.count())
        .where(Application.status.in_(VISIBLE_STATUSES))
        .group_by(Application.status)
    )
    by_status = {status: int(count) for status, count in rows.all()}

    async def _since(moment) -> int:
        res = await session.execute(
            select(func.count())
            .select_from(Application)
            .where(Application.status.in_(VISIBLE_STATUSES), Application.submitted_at >= moment)
        )
        return int(res.scalar_one())

    return {
        "total": sum(by_status.values()),
        "pending": by_status.get(Status.PENDING, 0),
        "approved": by_status.get(Status.APPROVED, 0),
        "rejected": by_status.get(Status.REJECTED, 0),
        "today": await _since(day_start),
        "week": await _since(week_start),
    }


# ------------------------------------------------------------------ answers / media
async def get_answers(session: AsyncSession, app_id: int) -> list[Answer]:
    result = await session.execute(
        select(Answer).where(Answer.application_id == app_id).order_by(Answer.position, Answer.id)
    )
    return list(result.scalars().all())


async def save_answer(
    session: AsyncSession,
    app_id: int,
    question: Question,
    position: int,
    text: str | None = None,
    complete: bool = True,
    skipped: bool = False,
) -> Answer:
    """Создаёт или обновляет ответ на вопрос (уникален по заявке + ключу вопроса)."""
    result = await session.execute(
        select(Answer).where(Answer.application_id == app_id, Answer.question_key == question.key)
    )
    answer = result.scalar_one_or_none()
    if answer is None:
        answer = Answer(application_id=app_id, question_key=question.key)
        session.add(answer)
    answer.question_label = question.label
    answer.question_text = question.text
    answer.position = position
    answer.text_value = text
    answer.is_complete = complete
    answer.skipped = skipped
    await session.flush()
    return answer


async def complete_answer(session: AsyncSession, answer: Answer) -> None:
    answer.is_complete = True
    await session.flush()


async def add_media(session: AsyncSession, app_id: int, answer_id: int, info, caption: str | None) -> Media:
    media = Media(
        application_id=app_id,
        answer_id=answer_id,
        kind=info.kind,
        file_id=info.file_id,
        file_unique_id=info.file_unique_id,
        file_name=info.file_name,
        mime_type=info.mime_type,
        file_size=info.file_size,
        caption=caption,
    )
    session.add(media)
    await session.flush()
    return media


async def count_media(session: AsyncSession, answer_id: int) -> int:
    result = await session.execute(select(func.count()).select_from(Media).where(Media.answer_id == answer_id))
    return int(result.scalar_one())


async def get_media(session: AsyncSession, app_id: int) -> list[Media]:
    result = await session.execute(select(Media).where(Media.application_id == app_id).order_by(Media.id))
    return list(result.scalars().all())


# ------------------------------------------------------------ список заявок пользователя
async def get_list_message(session: AsyncSession, user_id: int) -> UserListMessage | None:
    return await session.get(UserListMessage, user_id)


async def set_list_message(session: AsyncSession, user_id: int, chat_id: int, message_id: int) -> None:
    row = await session.get(UserListMessage, user_id)
    if row is None:
        session.add(UserListMessage(user_id=user_id, chat_id=chat_id, message_id=message_id))
    else:
        row.chat_id, row.message_id, row.updated_at = chat_id, message_id, utcnow()
    await session.flush()
