"""Ежесуточное списание за хостинг."""
from __future__ import annotations

import asyncio
import logging

from creator.money import rub
from creator.services import Ctx

log = logging.getLogger("billing")

CHECK_EVERY = 600


async def charge_once(ctx: Ctx, notify) -> None:
    day_price = ctx.price("HOSTING_DAY_PRICE")
    for b, ok in ctx.db.charge_due_bots(day_price):
        if ok:
            left = b["hosting_balance"] - day_price
            if day_price and left < day_price * 2:
                await notify(b["owner_id"], f"⏳ На хостинге бота ID {b['id']} осталось {rub(left)} — "
                                            f"это меньше 2 дней. Пополните в карточке бота → «💳 Хостинг».")
        else:
            await ctx.sup.stop(b["id"])
            await notify(b["owner_id"], f"⛔ Бот ID {b['id']} остановлен: закончился баланс хостинга. "
                                        "Пополните хостинг и нажмите «▶️ Запустить».")
    ctx.db.cleanup_drafts()


async def billing_loop(ctx: Ctx, notify) -> None:
    while True:
        try:
            await charge_once(ctx, notify)
        except Exception:  # noqa: BLE001
            log.exception("billing failed")
        await asyncio.sleep(CHECK_EVERY)
