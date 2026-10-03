"""Роли и права доступа."""
from __future__ import annotations

from enum import IntEnum

from sqlalchemy.ext.asyncio import AsyncSession

from appbot.db import repo


class Role(IntEnum):
    ADMIN = 1    # обычный администратор: работа с заявками
    MANAGER = 2  # администратор с правом управлять другими администраторами
    MAIN = 3     # главный администратор (из конфига): все права


ROLE_TITLES = {
    Role.MAIN: "👑 главный администратор",
    Role.MANAGER: "⭐ администратор с правом управления админами",
    Role.ADMIN: "🛡 администратор",
}


async def get_role(session: AsyncSession, user_id: int, main_admin_id: int) -> Role | None:
    """None — обычный пользователь. Проверяется в БД при каждом обращении."""
    if user_id == main_admin_id:
        return Role.MAIN
    admin = await repo.get_admin(session, user_id)
    if admin is None:
        return None
    return Role.MANAGER if admin.is_manager else Role.ADMIN


def can_manage_admins(role: Role | None) -> bool:
    return role is not None and role >= Role.MANAGER
