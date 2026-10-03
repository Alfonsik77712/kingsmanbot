"""Список заявок пользователя: сборка текста и автоматическое обновление уже отправленного сообщения."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from appbot.config import Settings
from appbot.db import repo
from appbot.db.models import STATUS_TITLES
from appbot.keyboards import kb_my_list
from appbot.utils import esc, fmt_dt, truncate

log = logging.getLogger(__name__)


async def build_user_list(session: AsyncSession, user_id: int, settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    """Актуальный список заявок ТОЛЬКО этого пользователя (выборка жёстко по его Telegram ID)."""
    apps = await repo.list_user_applications(session, user_id, limit=20)
    draft = await repo.get_draft(session, user_id)

    if not apps and not draft:
        return "У вас пока нет заявок. Чтобы подать заявку — /apply", kb_my_list()

    lines = ["<b>Ваши заявки</b>", ""]
    if draft:
        lines.append(f"✏️ #{draft.id} — не отправлена (продолжить: /apply, отменить: /cancel)")
    for a in apps:
        lines.append(f"#{a.id} — {STATUS_TITLES.get(a.status, a.status)} — {fmt_dt(a.submitted_at, settings.timezone)}")
        if a.admin_comment:
            lines.append(f"   💬 {esc(truncate(a.admin_comment, 200))}")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    lines += ["", f"<i>Обновлено: {fmt_dt(now, settings.timezone)}</i>"]
    return "\n".join(lines), kb_my_list()


async def refresh_user_list(bot: Bot, session: AsyncSession, settings: Settings, user_id: int) -> None:
    """Если пользователь открывал /myapplications — обновляет то сообщение (статус сменился / подана заявка)."""
    row = await repo.get_list_message(session, user_id)
    if row is None:
        return
    text, kb = await build_user_list(session, user_id, settings)
    try:
        await bot.edit_message_text(text, chat_id=row.chat_id, message_id=row.message_id, reply_markup=kb)
    except TelegramAPIError as exc:
        # Сообщение удалено/недоступно/не изменилось — не критично
        log.debug("Не удалось обновить список заявок пользователя %s: %s", user_id, exc)
