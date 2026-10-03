"""Последний роутер: всё, что не разобрали остальные."""
from aiogram import F, Router
from aiogram.types import Message

router = Router(name="fallback")
router.message.filter(F.chat.type == "private")


@router.message()
async def unknown(message: Message) -> None:
    if message.text and message.text.startswith("/"):
        await message.answer("Неизвестная команда. Список команд: /help")
