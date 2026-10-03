"""Типизированные callback_data для inline-кнопок (лимит Telegram — 64 байта)."""
from aiogram.filters.callback_data import CallbackData


class ListCb(CallbackData, prefix="l"):
    """Страница списка заявок."""
    flt: str = "all"   # all / pending / approved / rejected
    page: int = 1


class ViewCb(CallbackData, prefix="v"):
    """Открыть карточку заявки."""
    app_id: int
    flt: str = "all"
    page: int = 1
    media: int = 0     # 1 — дополнительно отправить медиафайлы (и длинные ответы)


class StatusCb(CallbackData, prefix="s"):
    """Сменить статус заявки."""
    app_id: int
    status: str
    flt: str = "all"
    page: int = 1


class HistoryCb(CallbackData, prefix="h"):
    app_id: int
    flt: str = "all"
    page: int = 1


class CommentCb(CallbackData, prefix="c"):
    """Кнопки при вводе комментария администратора: skip / cancel."""
    action: str


class DraftCb(CallbackData, prefix="d"):
    """Кнопки пользователя при заполнении: done / skip / submit / cancel."""
    action: str


class MyListCb(CallbackData, prefix="m"):
    """Кнопка «Обновить» в списке заявок пользователя."""
    action: str = "refresh"
