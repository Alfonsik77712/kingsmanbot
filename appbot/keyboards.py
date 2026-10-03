"""Inline-клавиатуры."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from appbot.callbacks import CommentCb, DraftCb, HistoryCb, ListCb, MyListCb, StatusCb, ViewCb
from appbot.db.models import Application, Status
from appbot.questions import Question

FILTERS = [("all", "Все"), ("pending", "🟡"), ("approved", "🟢"), ("rejected", "🔴")]
FILTER_NAMES = {
    "all": "Все",
    "pending": "🟡 На рассмотрении",
    "approved": "🟢 Одобренные",
    "rejected": "🔴 Отклонённые",
}


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# ----------------------------------------------------------------------- пользователь
def kb_prompt(q: Question, files_count: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if q.multiple and files_count > 0:
        b.row(_btn(f"✅ Готово ({files_count})", DraftCb(action="done").pack()))
    if not q.required and files_count == 0:
        b.row(_btn("⏭ Пропустить", DraftCb(action="skip").pack()))
    b.row(_btn("❌ Отменить заявку", DraftCb(action="cancel").pack()))
    return b.as_markup()


def kb_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        _btn("✅ Отправить заявку", DraftCb(action="submit").pack()),
        _btn("❌ Отменить", DraftCb(action="cancel").pack()),
    )
    return b.as_markup()


# ------------------------------------------------------------------- администратор
def kb_applications(rows: list[tuple[int, str]], flt: str, page: int, pages: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for app_id, title in rows:
        b.row(_btn(title, ViewCb(app_id=app_id, flt=flt, page=page).pack()))
    b.row(*[
        _btn(f"• {label}" if code == flt else label, ListCb(flt=code, page=1).pack())
        for code, label in FILTERS
    ])
    prev_btn = (
        _btn("⬅️ Назад", ListCb(flt=flt, page=page - 1).pack()) if page > 1 else _btn("⬅️ Назад", "noop:first")
    )
    next_btn = (
        _btn("Вперёд ➡️", ListCb(flt=flt, page=page + 1).pack()) if page < pages else _btn("Вперёд ➡️", "noop:last")
    )
    b.row(prev_btn, _btn(f"{page}/{pages}", "noop:page"), next_btn)
    b.row(_btn("🔄 Обновить", ListCb(flt=flt, page=page).pack()))
    return b.as_markup()


def kb_card(app: Application, flt: str, page: int) -> InlineKeyboardMarkup:
    def st(status: str) -> str:
        return StatusCb(app_id=app.id, status=status, flt=flt, page=page).pack()

    b = InlineKeyboardBuilder()
    top = []
    if app.status != Status.APPROVED:
        top.append(_btn("🟢 Одобрить", st(Status.APPROVED)))
    if app.status != Status.REJECTED:
        top.append(_btn("🔴 Отклонить", st(Status.REJECTED)))
    if top:
        b.row(*top)
    if app.status != Status.PENDING:
        b.row(_btn("🟡 Вернуть на рассмотрение", st(Status.PENDING)))
    b.row(
        _btn("📜 История", HistoryCb(app_id=app.id, flt=flt, page=page).pack()),
        _btn("⬅️ К списку", ListCb(flt=flt, page=page).pack()),
    )
    return b.as_markup()


def kb_history_back(app_id: int, flt: str, page: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("⬅️ К заявке", ViewCb(app_id=app_id, flt=flt, page=page).pack()))
    return b.as_markup()


def kb_comment_prompt() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        _btn("Без комментария", CommentCb(action="skip").pack()),
        _btn("Отмена", CommentCb(action="cancel").pack()),
    )
    return b.as_markup()


def kb_open_application(app_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("📂 Открыть заявку", ViewCb(app_id=app_id, media=1).pack()))
    return b.as_markup()


def kb_my_list() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("🔄 Обновить", MyListCb(action="refresh").pack()))
    return b.as_markup()
