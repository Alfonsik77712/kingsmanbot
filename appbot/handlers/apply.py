"""Подача заявки: пошаговая анкета.

Состояние заполнения хранится в БД (заявка со статусом draft + сохранённые ответы), а не в памяти.
Следующий вопрос определяется как первый вопрос шаблона без завершённого ответа —
поэтому заполнение продолжается после перезапуска бота и даже после правки шаблона.
"""
from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from appbot.callbacks import DraftCb
from appbot.config import Settings
from appbot.db import repo
from appbot.db.models import Answer, Application, Status, User
from appbot.keyboards import kb_confirm, kb_open_application, kb_prompt
from appbot.media import allowed_kinds, extract_media, group_media_by_answer
from appbot.questions import APPLY_INTRO, QUESTIONS, QType, Question
from appbot.texts import question_hint, render_answers
from appbot.user_view import refresh_user_list
from appbot.utils import accessible, esc, not_command, safe_clear_markup, safe_send, split_message

log = logging.getLogger(__name__)

router = Router(name="apply")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

# Небольшие in-memory подсказки для альбомов (потеря при перезапуске безопасна)
_closed_groups: dict[int, str] = {}                 # user_id -> media_group_id, уже принятый для одиночного вопроса
_notices: dict[int, tuple[str | None, int]] = {}    # user_id -> (media_group_id, id сообщения «Принято файлов»)


def _remember(store: dict, key, value) -> None:
    if len(store) > 5000:
        store.clear()
    store[key] = value


# ------------------------------------------------------------------ состояние анкеты
async def load_state(session: AsyncSession, app: Application) -> tuple[dict[str, Answer], Question | None]:
    """Возвращает ответы и текущий вопрос (None — все вопросы пройдены, пора подтверждать)."""
    answers = {a.question_key: a for a in await repo.get_answers(session, app.id)}
    for q in QUESTIONS:
        answer = answers.get(q.key)
        if answer is None or not answer.is_complete:
            return answers, q
    return answers, None


async def send_prompt(bot: Bot, chat_id: int, session: AsyncSession, app: Application) -> None:
    """Задаёт текущий вопрос (или показывает итог, если вопросы закончились)."""
    answers, q = await load_state(session, app)
    if q is None:
        await send_summary(bot, chat_id, session, app)
        return
    files = 0
    if q.multiple and q.key in answers:
        files = await repo.count_media(session, answers[q.key].id)
    idx = QUESTIONS.index(q) + 1
    text = f"<b>Вопрос {idx}/{len(QUESTIONS)}</b>\n\n{esc(q.text)}"
    hint = question_hint(q)
    if hint:
        text += f"\n\n<i>{esc(hint)}</i>"
    await safe_send(bot, chat_id, text, reply_markup=kb_prompt(q, files))


async def send_summary(bot: Bot, chat_id: int, session: AsyncSession, app: Application) -> None:
    answers = await repo.get_answers(session, app.id)
    media = group_media_by_answer(await repo.get_media(session, app.id))
    text = "<b>Проверьте вашу заявку</b>\n\n" + render_answers(answers, media)
    chunks = split_message(text)
    for chunk in chunks[:-1]:
        await safe_send(bot, chat_id, chunk)
    await safe_send(bot, chat_id, chunks[-1] + "\n\n<b>Отправить заявку?</b>", reply_markup=kb_confirm())


# ------------------------------------------------------------------------ команды
@router.message(Command("apply"))
async def cmd_apply(message: Message, session: AsyncSession, bot: Bot, settings: Settings, db_user: User) -> None:
    uid = message.from_user.id

    draft = await repo.get_draft(session, uid)
    if draft:
        await message.answer(
            f"У вас уже есть незавершённая заявка <b>#{draft.id}</b>. Продолжайте заполнение "
            "или отмените её командой /cancel."
        )
        await send_prompt(bot, message.chat.id, session, draft)
        return

    limit = settings.max_pending_per_user
    if limit and await repo.count_user_applications(session, uid, Status.PENDING) >= limit:
        await message.answer(
            f"У вас уже есть заявки на рассмотрении (максимум — {limit}). "
            "Дождитесь решения по ним, статус можно посмотреть в /myapplications."
        )
        return

    draft = await repo.create_draft(session, db_user)
    if draft is None:
        # Параллельный /apply успел создать черновик раньше — просто продолжаем его
        draft = await repo.get_draft(session, uid)
        if draft is None:
            await message.answer("Не удалось начать заявку, попробуйте ещё раз.")
            return
        await message.answer("У вас уже есть незавершённая заявка — продолжаем её.")
        await send_prompt(bot, message.chat.id, session, draft)
        return

    await session.commit()
    log.info("Пользователь %s начал заявку #%s", uid, draft.id)
    await message.answer(APPLY_INTRO)
    await send_prompt(bot, message.chat.id, session, draft)


async def _cancel(session: AsyncSession, user_id: int) -> Application | None:
    app = await repo.cancel_draft(session, user_id)
    if app:
        log.info("Пользователь %s отменил заполнение заявки #%s", user_id, app.id)
    return app


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, session: AsyncSession) -> None:
    app = await _cancel(session, message.from_user.id)
    if app:
        await message.answer(f"Заполнение заявки #{app.id} отменено. Чтобы начать заново — /apply.")
    else:
        await message.answer("У вас нет заявки в процессе заполнения. Чтобы подать заявку — /apply.")


# --------------------------------------------------------------------- кнопки
@router.callback_query(DraftCb.filter(F.action == "cancel"))
async def cb_cancel(cb: CallbackQuery, session: AsyncSession) -> None:
    app = await _cancel(session, cb.from_user.id)
    msg = accessible(cb)
    if msg:
        await safe_clear_markup(msg)
    if app:
        await cb.answer("Заявка отменена")
        if msg:
            await msg.answer(f"Заполнение заявки #{app.id} отменено. Чтобы начать заново — /apply.")
    else:
        await cb.answer("Нет активной заявки", show_alert=True)


@router.callback_query(DraftCb.filter(F.action == "skip"))
async def cb_skip(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    app = await repo.get_draft(session, cb.from_user.id)
    if app is None:
        await cb.answer("Нет активной заявки. Начать: /apply", show_alert=True)
        return
    _, q = await load_state(session, app)
    if q is None or q.required:
        await cb.answer("Этот вопрос нельзя пропустить.", show_alert=True)
        return
    await repo.save_answer(session, app.id, q, QUESTIONS.index(q), text=None, complete=True, skipped=True)
    await cb.answer("Пропущено")
    msg = accessible(cb)
    if msg:
        await safe_clear_markup(msg)
    await send_prompt(bot, cb.from_user.id, session, app)


@router.callback_query(DraftCb.filter(F.action == "done"))
async def cb_done(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """Завершение вопроса с несколькими файлами."""
    app = await repo.get_draft(session, cb.from_user.id)
    if app is None:
        await cb.answer("Нет активной заявки. Начать: /apply", show_alert=True)
        return
    answers, q = await load_state(session, app)
    if q is None or not q.multiple:
        await cb.answer("Сейчас это действие недоступно.", show_alert=True)
        return
    answer = answers.get(q.key)
    count = await repo.count_media(session, answer.id) if answer else 0
    if count == 0:
        if q.required:
            await cb.answer("Сначала отправьте хотя бы один файл.", show_alert=True)
            return
        await repo.save_answer(session, app.id, q, QUESTIONS.index(q), complete=True, skipped=True)
    elif count < q.min_files:
        await cb.answer(f"Нужно минимум файлов: {q.min_files} (сейчас {count}).", show_alert=True)
        return
    else:
        await repo.complete_answer(session, answer)
    await cb.answer("Готово")
    msg = accessible(cb)
    if msg:
        await safe_clear_markup(msg)
    await send_prompt(bot, cb.from_user.id, session, app)


@router.callback_query(DraftCb.filter(F.action == "submit"))
async def cb_submit(
    cb: CallbackQuery, session: AsyncSession, bot: Bot, settings: Settings, db_user: User
) -> None:
    app = await repo.get_draft(session, cb.from_user.id)
    if app is None:
        await cb.answer("Нет активной заявки (возможно, она уже отправлена).", show_alert=True)
        return
    _, q = await load_state(session, app)
    if q is not None:
        await cb.answer("Заполнены не все вопросы.", show_alert=True)
        await send_prompt(bot, cb.from_user.id, session, app)
        return

    await repo.submit_application(session, app, db_user)
    await session.commit()  # заявка зафиксирована в БД ДО отправки уведомлений
    log.info("Заявка #%s отправлена пользователем %s", app.id, cb.from_user.id)

    await cb.answer("Заявка отправлена")
    msg = accessible(cb)
    if msg:
        await safe_clear_markup(msg)
    await safe_send(bot, cb.from_user.id, f"✅ Ваша заявка #{app.id} успешно отправлена и передана на рассмотрение.")
    await refresh_user_list(bot, session, settings, cb.from_user.id)

    if settings.notify_admins_on_new:
        await _notify_admins(bot, session, settings, app)


async def _notify_admins(bot: Bot, session: AsyncSession, settings: Settings, app: Application) -> None:
    ids = set(await repo.list_admin_ids(session)) | {settings.main_admin_id}
    ids.discard(app.user_id)  # не уведомляем автора, если он сам админ
    name = esc(app.full_name or app.username or app.user_id)
    text = f"📥 <b>Новая заявка #{app.id}</b>\nОт: {name}"
    await asyncio.gather(
        *(safe_send(bot, uid, text, reply_markup=kb_open_application(app.id)) for uid in ids),
        return_exceptions=True,
    )


# ------------------------------------------------------------------ ответы пользователя
def _validate_text(q: Question, text: str) -> str | None:
    if len(text) > q.max_length:
        return f"Слишком длинный ответ: {len(text)} символов, максимум — {q.max_length}."
    if q.pattern and not re.search(q.pattern, text):
        return q.pattern_error
    return None


async def _notify_files(message: Message, bot: Bot, q: Question, count: int) -> None:
    """Сообщение «Принято файлов: N». Для альбома обновляется одно сообщение, а не плодится N новых."""
    uid = message.from_user.id
    gid = message.media_group_id
    text = f"📎 Принято файлов: {count}/{q.max_files}. Отправьте ещё или нажмите «Готово»."
    kb = kb_prompt(q, count)
    prev = _notices.get(uid)
    if gid and prev and prev[0] == gid:
        try:
            await bot.edit_message_text(text, chat_id=message.chat.id, message_id=prev[1], reply_markup=kb)
            return
        except TelegramAPIError:
            pass
    sent = await safe_send(bot, message.chat.id, text, reply_markup=kb)
    if sent:
        _remember(_notices, uid, (gid, sent.message_id))


async def _handle_answer(
    message: Message, session: AsyncSession, bot: Bot, app: Application, answers: dict[str, Answer], q: Question
) -> None:
    uid = message.from_user.id
    pos = QUESTIONS.index(q)
    t = q.type

    if t == QType.TEXT:
        text = (message.text or "").strip()
        if not text:
            await message.answer("Пожалуйста, ответьте текстовым сообщением.")
            return
        error = _validate_text(q, text)
        if error:
            await message.answer(error)
            return
        await repo.save_answer(session, app.id, q, pos, text=text)

    elif t == QType.NUMBER:
        raw = (message.text or "").strip().replace(" ", "")
        if not re.fullmatch(r"-?\d+", raw):
            await message.answer("Нужно отправить целое число, например: 18")
            return
        value = int(raw)
        if (q.min_value is not None and value < q.min_value) or (q.max_value is not None and value > q.max_value):
            await message.answer(question_hint(q) or "Число вне допустимого диапазона.")
            return
        await repo.save_answer(session, app.id, q, pos, text=str(value))

    elif t == QType.ANY:
        text = (message.text or message.caption or "").strip()
        info = extract_media(message)
        if not text and info is None:
            await message.answer("Отправьте текст или файл (фото, видео, документ).")
            return
        if text and len(text) > q.max_length:
            await message.answer(f"Слишком длинный текст: {len(text)} символов, максимум — {q.max_length}.")
            return
        answer = await repo.save_answer(session, app.id, q, pos, text=text or None)
        if info:
            await repo.add_media(session, app.id, answer.id, info, message.caption)

    else:  # PHOTO / VIDEO / DOCUMENT / MEDIA
        gid = message.media_group_id
        if gid and _closed_groups.get(uid) == gid:
            return  # остальные файлы альбома, отправленного на вопрос с одним файлом
        info = extract_media(message)
        if info is None or info.kind not in allowed_kinds(q):
            await message.answer("Этот тип сообщения не подходит.\n" + (question_hint(q) or ""))
            return

        if q.multiple:
            answer = answers.get(q.key) or await repo.save_answer(session, app.id, q, pos, complete=False)
            count = await repo.count_media(session, answer.id)
            if count >= q.max_files:
                await message.answer(
                    f"Достигнут лимит — {q.max_files} файлов. Нажмите «Готово», чтобы продолжить.",
                    reply_markup=kb_prompt(q, count),
                )
                return
            await repo.add_media(session, app.id, answer.id, info, message.caption)
            await _notify_files(message, bot, q, count + 1)
            return  # вопрос завершится кнопкой «Готово»

        answer = await repo.save_answer(session, app.id, q, pos, text=None)
        await repo.add_media(session, app.id, answer.id, info, message.caption)
        if gid:
            _remember(_closed_groups, uid, gid)

    await send_prompt(bot, message.chat.id, session, app)


@router.message(not_command)
async def on_message(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Любое не-командное сообщение трактуется как ответ на текущий вопрос анкеты."""
    app = await repo.get_draft(session, message.from_user.id)
    if app is None:
        await message.answer("У вас нет заявки в процессе заполнения. Чтобы подать заявку — /apply.")
        return
    answers, q = await load_state(session, app)
    if q is None:
        # Все ответы собраны — ждём подтверждения (например, после перезапуска бота)
        await send_summary(bot, message.chat.id, session, app)
        return
    await _handle_answer(message, session, bot, app, answers, q)
