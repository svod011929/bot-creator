"""Boostly — доп. заработок в режиме трансляции.

Хаб-ключ площадки остаётся только в конструкторе (env → админка → прежний файл)
и никогда не записывается в дочерний бот. Задания и проверка идут через локальный
прокси (см. creator.boostly_tasks). Награда владельцу — 50 копеек за задание
со статусом confirmed, начисляется атомарно на стороне конструктора.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from creator.db import now

if TYPE_CHECKING:
    from creator.services import Ctx

log = logging.getLogger("boostly")


class BoostlyError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def load_hub_key(cfg) -> str:
    """Constructor Boostly hub key. Never a child token."""
    from creator.integration_keys import value
    return value(cfg, "boostly_hub")


class BoostlyService:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        self.db = ctx.db

    def hub_key(self) -> str:
        return load_hub_key(self.ctx.cfg)

    def reward(self) -> int:
        """Награда владельцу за 1 подтверждённое задание, копейки."""
        return int(self.ctx.price("BOOSTLY_REWARD"))

    def min_users(self) -> int:
        return int(self.ctx.price("BOOSTLY_MIN_USERS"))

    def users(self, bot_id: int) -> int:
        from creator.tgrass import child_users_count
        return child_users_count(self.ctx.sup.bot_dir(bot_id))

    async def approve(self, bot_id: int, admin_id: int) -> None:
        row = self.db.boostly(bot_id)
        b = self.db.bot(bot_id)
        if not row or not b:
            raise LookupError("заявка не найдена")
        if row["status"] == "approved":
            return
        if not self.hub_key():
            self.db.boostly_set(bot_id, error="хаб-ключ Boostly не настроен")
            raise BoostlyError("хаб-ключ Boostly не настроен. Задайте его в «🔑 Ключи интеграций».")
        self.db.boostly_set(
            bot_id,
            status="approved",
            error=None,
            decided_at=now(),
            decided_by=admin_id,
        )
        await self.ctx.apply_changes(bot_id)
        log.info("bot %s подключён к Boostly (админ %s)", bot_id, admin_id)

    async def reject(self, bot_id: int, admin_id: int) -> bool:
        row = self.db.boostly(bot_id)
        if not row or row["status"] != "pending":
            return False
        self.db.boostly_set(bot_id, status="rejected", decided_at=now(), decided_by=admin_id)
        return True

    async def disable(self, bot_id: int, admin_id: int) -> bool:
        row = self.db.boostly(bot_id)
        if not row or row["status"] != "approved":
            return False
        self.db.boostly_set(bot_id, status="disabled", decided_at=now(), decided_by=admin_id)
        await self.ctx.apply_changes(bot_id)
        return True
