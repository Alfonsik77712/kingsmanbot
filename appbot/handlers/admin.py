"""Админ-раздел: список заявок, карточка, смена статуса, статистика, управление администраторами.

Доступ ограничен AdminOnlyMiddleware: проверка прав выполняется в БД перед КАЖДЫМ действием.
"""
from __future__ import annotations

import logging
from math import ceil

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from appbot import permissions
from appbot.callbacks import CommentCb, HistoryCb, ListCb, StatusCb, ViewCb
from appbot.commands import grant_commands, revoke_commands
from appbot.config import Settings
from appbot.db import repo
from appbot.db.models import STATUS_EMOJI, STATUS_TITLES, VISIBLE_STATUSES, Application, Status
from appbot.keyboards import (
    FILTER_NAMES,
    kb_applications,
    kb_card,
    kb_comment_prompt,
    kb_history_back,
)
from appbot.media import PHOTO, group_media_by_answer, send_application_media
from appbot.middlewares import AdminOnlyMiddleware
from appbot.permissions import ROLE_TITLES, Role, can_manage_admins
from appbot.questions import APPLICATION_TITLE_KEY
from appbot.texts import (
    render_answers,
    render_header,
    render_history,
    status_notification,
)
from appbot.user_view import refresh_user_list
from appbot.utils import (
    CAPTION_LIMIT,
    accessible,
    day_start_utc,
    esc,
    not_command,
    safe_send,
    show_screen,
    split_message,
    truncate,
    week_start_utc,
)

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

CHANGEABLE = {Status.PENDING, Status.APPROVED, Status.REJECTED}
MAX_COMMENT_LEN = 1000


class AdminStates(StatesGroup):
    waiting_comment = State()  # ожидаем комментарий администратора к смене статуса


def _valid_filter(flt: str) -> str:
    return flt if flt in FILTER_NAMES else "all"


# ================================================================= список заявок
def _app_title(app: Application, titles: dict[int, str]) -> str:
    raw = titles.get(app.id) or (f"@{app.username}" if app.username else None) or app.full_name or str(app.user_id)
    return truncate(raw.replace("\n", " "), 28)


async def build_list(session: AsyncSession, settings: Settings, flt: str, page: int):
    flt = _valid_filter(flt)
    status = None if flt == "all" else flt
    total = await repo.count_applications(session, status)
    pages = max(1, ceil(total / settings.page_size))
    page = min(max(page, 1), pages)
    apps = await repo.list_applications(session, status, page, settings.page_size)
    titles = await repo.get_titles(session, [a.id for a in apps], APPLICATION_TITLE_KEY)

    text = f"<b>Заявки — страница {page}/{pages}</b>\nФильтр: {FILTER_NAMES[flt]} · всего: {total}"
    if not apps:
        text += "\n\nЗаявок пока нет."
    rows = [(a.id, f"#{a.id} — {STATUS_EMOJI[a.status]} {_app_title(a, titles)}") for a in apps]
    return text, kb_applications(rows, flt, page, pages)


@router.message(Command("applications"))
async def cmd_applications(message: Message, session: AsyncSession, settings: Settings) -> None:
    text, kb = await build_list(session, settings, "all", 1)
    await message.answer(text, reply_markup=kb)


@router.callback_query(ListCb.filter())
async def cb_list(
    cb: CallbackQuery, callback_data: ListCb, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    msg = accessible(cb)
    if msg is None:
        await cb.answer("Сообщение устарело. Откройте список заново: /applications", show_alert=True)
        return
    text, kb = await build_list(session, settings, callback_data.flt, callback_data.page)
    # Если открыта карточка с фото — она заменяется текстовым списком (удалить + отправить)
    changed = await show_screen(bot, msg.chat.id, msg.message_id, bool(msg.photo), text, kb) is not None
    await cb.answer("Обновлено" if changed else "Без изменений")


@router.callback_query(F.data.startswith("noop:"))
async def cb_noop(cb: CallbackQuery) -> None:
    hints = {"noop:first": "Это первая страница", "noop:last": "Это последняя страница"}
    await cb.answer(hints.get(cb.data or "", ""))


# ================================================================ карточка заявки
async def build_card(session: AsyncSession, settings: Settings, app_id: int):
    """Возвращает (заявка, текст, доп. сообщения с ответами, ответы, медиа, фото для карточки) либо None.

    Фото для карточки — первый скриншот заявки: карточка показывается как фото-сообщение,
    а текст идёт в подписи (лимит 1024 символа). Если текст не помещается в подпись,
    фото в карточку не вкладывается (None) и отправляется отдельным сообщением.
    """
    app = await repo.get_application(session, app_id)
    if app is None or app.status not in VISIBLE_STATUSES:
        return None
    answers = await repo.get_answers(session, app.id)
    media = await repo.get_media(session, app.id)
    photo = next((m for m in media if m.kind == PHOTO), None)
    body = render_answers(answers, group_media_by_answer(media))
    header = render_header(app, settings)
    full = f"{header}\n\n<b>Ответы:</b>\n\n{body}"
    limit = CAPTION_LIMIT if photo else 4000
    if len(full) <= limit:
        return app, full, [], answers, media, photo
    # Ответы не помещаются в одно сообщение — карточка без них, а ответы целиком идут отдельными сообщениями
    note = "\n\n<i>Ответы не поместились в карточку — они отправлены отдельными сообщениями.</i>"
    overflow = split_message(f"<b>Ответы заявки #{app.id}:</b>\n\n{body}")
    card = header + note
    if photo and len(card) > CAPTION_LIMIT:
        photo = None  # даже шапка не влезает в подпись — карточка текстом, фото отдельным сообщением
    return app, card, overflow, answers, media, photo


async def refresh_card(
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    chat_id: int,
    message_id: int,
    app_id: int,
    flt: str,
    page: int,
    cur_photo: bool = False,
) -> None:
    built = await build_card(session, settings, app_id)
    if built:
        app, text, _overflow, _answers, _media, photo = built
        await show_screen(
            bot, chat_id, message_id, cur_photo, text, kb_card(app, flt, page), photo.file_id if photo else None
        )


@router.callback_query(ViewCb.filter())
async def cb_view(
    cb: CallbackQuery, callback_data: ViewCb, session: AsyncSession, bot: Bot, settings: Settings
) -> None:
    msg = accessible(cb)
    if msg is None:
        await cb.answer("Сообщение устарело. Откройте список заново: /applications", show_alert=True)
        return
    built = await build_card(session, settings, callback_data.app_id)
    if built is None:
        await cb.answer("Заявка не найдена.", show_alert=True)
        return
    app, text, overflow, answers, media, photo = built
    flt = _valid_filter(callback_data.flt)
    await cb.answer()
    # Скриншот показывается прямо в карточке (фото-сообщение с подписью и кнопками)
    await show_screen(
        bot, msg.chat.id, msg.message_id, bool(msg.photo), text,
        kb_card(app, flt, callback_data.page), photo.file_id if photo else None,
    )
    log.info("Администратор %s открыл заявку #%s", cb.from_user.id, app.id)

    for chunk in overflow:
        await safe_send(bot, msg.chat.id, chunk)
    # Остальные файлы (всё, что не вложено в карточку) — по запросу (кнопка из уведомления)
    # либо всегда, если скриншот не удалось вложить в карточку
    has_photo = any(m.kind == PHOTO for m in media)
    if callback_data.media or (photo is None and has_photo):
        rest = [m for m in media if photo is None or m.id != photo.id]
        if rest:
            await send_application_media(bot, msg.chat.id, answers, rest)


@router.callback_query(HistoryCb.filter())
async def cb_history(
    cb: CallbackQuery, callback_data: HistoryCb, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    msg = accessible(cb)
    app = await repo.get_application(session, callback_data.app_id)
    if msg is None or app is None or app.status not in VISIBLE_STATUSES:
        await cb.answer("Заявка не найдена.", show_alert=True)
        return
    history = await repo.get_history(session, app.id)
    text = render_history(app, history, settings)
    await show_screen(
        bot, msg.chat.id, msg.message_id, bool(msg.photo), text,
        kb_history_back(app.id, _valid_filter(callback_data.flt), callback_data.page),
    )
    await cb.answer()


# ============================================================= смена статуса
async def apply_status(
    bot: Bot, session: AsyncSession, settings: Settings, app_id: int, new_status: str, admin_id: int, comment: str | None
) -> tuple[Application | None, bool]:
    """Меняет статус, фиксирует в БД и уведомляет пользователя. Возвращает (заявка, доставлено ли уведомление)."""
    result = await repo.change_status(session, app_id, new_status, admin_id, comment)
    if result is None:
        return None, False
    app, old = result
    await session.commit()
    log.info("Заявка #%s: %s -> %s (администратор %s, комментарий: %s)", app.id, old, new_status, admin_id, bool(comment))
    sent = await safe_send(bot, app.user_id, status_notification(app.id, new_status, comment))
    # Список «Ваши заявки» у пользователя тоже показывает актуальный статус
    await refresh_user_list(bot, session, settings, app.user_id)
    return app, sent is not None


@router.callback_query(StatusCb.filter())
async def cb_status(
    cb: CallbackQuery,
    callback_data: StatusCb,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    state: FSMContext,
) -> None:
    msg = accessible(cb)
    if msg is None:
        await cb.answer("Сообщение устарело. Откройте заявку заново.", show_alert=True)
        return
    new_status = callback_data.status
    app = await repo.get_application(session, callback_data.app_id)
    if new_status not in CHANGEABLE or app is None or app.status not in VISIBLE_STATUSES:
        await cb.answer("Заявка не найдена.", show_alert=True)
        return
    if app.status == new_status:
        await cb.answer("Заявка уже имеет этот статус.", show_alert=True)
        return

    flt = _valid_filter(callback_data.flt)
    if new_status == Status.PENDING:
        # Возврат на рассмотрение — без комментария, сразу
        _, delivered = await apply_status(bot, session, settings, app.id, new_status, cb.from_user.id, None)
        await refresh_card(
            bot, session, settings, msg.chat.id, msg.message_id, app.id, flt, callback_data.page, bool(msg.photo)
        )
        await cb.answer("Возвращена на рассмотрение" + ("" if delivered else " (пользователь недоступен)"))
        return

    # Одобрение / отклонение — предлагаем добавить комментарий
    await state.set_state(AdminStates.waiting_comment)
    await state.update_data(
        app_id=app.id, status=new_status, flt=flt, page=callback_data.page,
        chat_id=msg.chat.id, message_id=msg.message_id, is_photo=bool(msg.photo),
    )
    prompt = (
        f"{STATUS_TITLES[new_status]} — заявка #{app.id}\n\n"
        "Напишите комментарий для пользователя — он получит его вместе с уведомлением.\n"
        "Или нажмите «Без комментария»."
    )
    # Запрос комментария показываем на том же сообщении (у фото-карточки — в подписи, скрин остаётся на виду)
    photo_id = msg.photo[-1].file_id if msg.photo else None
    await show_screen(bot, msg.chat.id, msg.message_id, bool(msg.photo), prompt, kb_comment_prompt(), photo_id)
    await cb.answer()


async def _pending_comment_data(state: FSMContext) -> dict | None:
    if await state.get_state() != AdminStates.waiting_comment.state:
        return None
    return await state.get_data() or None


@router.callback_query(CommentCb.filter())
async def cb_comment(
    cb: CallbackQuery,
    callback_data: CommentCb,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    state: FSMContext,
) -> None:
    data = await _pending_comment_data(state)
    msg = accessible(cb)
    if data is None or msg is None:
        await cb.answer("Нет ожидающего действия. Откройте заявку заново.", show_alert=True)
        return
    await state.clear()

    if callback_data.action == "skip":
        app, delivered = await apply_status(bot, session, settings, data["app_id"], data["status"], cb.from_user.id, None)
        note = "Статус изменён" + ("" if delivered else " (пользователь недоступен)") if app else "Заявка не найдена"
    else:
        note = "Отменено"
    await refresh_card(
        bot, session, settings, data["chat_id"], data["message_id"], data["app_id"], data["flt"], data["page"],
        data.get("is_photo", False),
    )
    await cb.answer(note)


@router.message(Command("cancel"), AdminStates.waiting_comment)
async def cmd_cancel_comment(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, settings: Settings) -> None:
    data = await state.get_data()
    await state.clear()
    await message.answer("Действие отменено.")
    if data:
        await refresh_card(
            bot, session, settings, data["chat_id"], data["message_id"], data["app_id"], data["flt"], data["page"],
            data.get("is_photo", False),
        )


@router.message(AdminStates.waiting_comment, not_command)
async def on_comment(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, settings: Settings
) -> None:
    if not message.text:
        await message.answer("Отправьте комментарий текстом или нажмите «Без комментария».")
        return
    comment = message.text.strip()
    if len(comment) > MAX_COMMENT_LEN:
        await message.answer(f"Слишком длинный комментарий ({len(comment)} симв.), максимум — {MAX_COMMENT_LEN}.")
        return
    data = await state.get_data()
    await state.clear()
    app, delivered = await apply_status(bot, session, settings, data["app_id"], data["status"], message.from_user.id, comment)
    if app is None:
        await message.answer("Заявка не найдена — статус не изменён.")
        return
    await refresh_card(
        bot, session, settings, data["chat_id"], data["message_id"], app.id, data["flt"], data["page"],
        data.get("is_photo", False),
    )
    await message.answer(
        f"✅ Статус заявки #{app.id} изменён: {STATUS_TITLES[app.status]}.\n"
        + ("Пользователь уведомлён." if delivered else "⚠️ Уведомление не доставлено (пользователь заблокировал бота).")
    )


# =================================================================== статистика
@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession, settings: Settings) -> None:
    s = await repo.get_stats(session, day_start_utc(settings.timezone), week_start_utc())
    await message.answer(
        "<b>📊 Статистика заявок</b>\n\n"
        f"Всего: <b>{s['total']}</b>\n"
        f"🟡 На рассмотрении: <b>{s['pending']}</b>\n"
        f"🟢 Одобрено: <b>{s['approved']}</b>\n"
        f"🔴 Отклонено: <b>{s['rejected']}</b>\n\n"
        f"За сегодня: <b>{s['today']}</b>\n"
        f"За 7 дней: <b>{s['week']}</b>"
    )


# ========================================================== администраторы
@router.message(Command("admins"))
async def cmd_admins(message: Message, session: AsyncSession, settings: Settings) -> None:
    rows = await repo.list_admins(session)
    entries: list[tuple[int, Role, str]] = []
    seen: set[int] = set()
    main_id = settings.main_admin_id
    main_user = next((u for a, u in rows if a.user_id == main_id), None)
    entries.append((main_id, Role.MAIN, _display(main_user)))
    seen.add(main_id)
    for admin, user in rows:
        if admin.user_id in seen:
            continue
        seen.add(admin.user_id)
        entries.append((admin.user_id, Role.MANAGER if admin.is_manager else Role.ADMIN, _display(user)))

    lines = ["<b>Администраторы</b>", ""]
    for uid, role, name in entries:
        lines.append(f"<code>{uid}</code> {name} — {ROLE_TITLES[role]}")
    await message.answer("\n".join(lines))


def _display(user) -> str:
    if user is None:
        return "(ещё не запускал бота)"
    parts = [esc(user.full_name)] if user.full_name else []
    if user.username:
        parts.append(f"@{esc(user.username)}")
    return " ".join(parts) or "—"


def _parse_target(command: CommandObject) -> tuple[int | None, list[str]]:
    args = (command.args or "").split()
    if not args or not args[0].isdigit():
        return None, args
    return int(args[0]), args[1:]


@router.message(Command("addadmin"))
async def cmd_add_admin(
    message: Message, command: CommandObject, session: AsyncSession, bot: Bot, settings: Settings, role: Role
) -> None:
    if not can_manage_admins(role):
        await message.answer("⛔ Выдавать права администратора может только главный администратор.")
        return
    target, extra = _parse_target(command)
    if target is None:
        await message.answer(
            "Использование: <code>/addadmin 123456789</code>\n"
            + ("Чтобы дать право управлять администраторами: <code>/addadmin 123456789 manager</code>"
               if role == Role.MAIN else "")
        )
        return
    wants_manager = bool(extra) and extra[0].lower() in {"manager", "manage", "m"}
    if wants_manager and role != Role.MAIN:
        await message.answer("⛔ Право управления администраторами выдаёт только главный администратор.")
        return

    existing = await repo.get_admin(session, target)
    if target == settings.main_admin_id:
        await message.answer("Этот пользователь — главный администратор.")
        return
    if existing:
        if wants_manager and not existing.is_manager:
            existing.is_manager = True
            await session.flush()
            await grant_commands(bot, target, Role.MANAGER)
            log.info("Администратор %s получил право управления от %s", target, message.from_user.id)
            await message.answer(f"✅ Пользователь <code>{target}</code> теперь может управлять администраторами.")
        else:
            await message.answer(f"Пользователь <code>{target}</code> уже является администратором.")
        return

    await repo.add_admin(session, target, message.from_user.id, is_manager=wants_manager)
    await session.commit()
    new_role = Role.MANAGER if wants_manager else Role.ADMIN
    log.info("Администратор %s добавил администратора %s (%s)", message.from_user.id, target, new_role.name)
    await grant_commands(bot, target, new_role)
    notified = await safe_send(
        bot, target, "🛡 Вам выданы права администратора. Откройте /applications, чтобы просматривать заявки."
    )
    await message.answer(
        f"✅ Пользователь <code>{target}</code> добавлен: {ROLE_TITLES[new_role]}."
        + ("" if notified else "\n⚠️ Уведомить его не удалось — он ещё не запускал бота.")
    )


@router.message(Command("removeadmin"))
async def cmd_remove_admin(
    message: Message, command: CommandObject, session: AsyncSession, bot: Bot, settings: Settings, role: Role
) -> None:
    if not can_manage_admins(role):
        await message.answer("⛔ Снимать права администратора может только главный администратор.")
        return
    target, _ = _parse_target(command)
    if target is None:
        await message.answer("Использование: <code>/removeadmin 123456789</code>")
        return
    if target == settings.main_admin_id:
        await message.answer("⛔ Главного администратора удалить нельзя.")
        return
    admin = await repo.get_admin(session, target)
    if admin is None:
        await message.answer(f"Пользователь <code>{target}</code> не является администратором.")
        return
    if admin.is_manager and role != Role.MAIN:
        await message.answer("⛔ Снять права у администратора с правом управления может только главный администратор.")
        return

    await repo.remove_admin(session, target)
    await session.commit()
    log.info("Администратор %s удалил администратора %s", message.from_user.id, target)
    await revoke_commands(bot, target)
    await safe_send(bot, target, "Ваши права администратора были сняты.")
    await message.answer(f"✅ Пользователь <code>{target}</code> больше не администратор.")
