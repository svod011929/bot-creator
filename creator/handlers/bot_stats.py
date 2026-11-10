"""Owner-only statistics screen; reads one bot database, changes nothing."""
from __future__ import annotations

import asyncio
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from creator.bot_stats import read, render
from creator.engagement import request as activity_request
from creator.keyboards import btn, ikb
from creator.services import Ctx

router = Router(name="bot_stats")
REQUEST = re.compile(r"bst:([1-9][0-9]{0,18})")
UNAVAILABLE = "Бот не найден или действие устарело."
ACTIVE = "Завершите текущую настройку или нажмите /cancel."


def _owned(ctx: Ctx, bot_id: int, uid: int):
    row = ctx.db.bot(bot_id)
    return row if row is not None and not row["deleted"] and row["owner_id"] == uid else None


@router.callback_query(F.data.startswith("bst:"))
async def open_stats(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    match = REQUEST.fullmatch(cb.data or "")
    uid = cb.from_user.id
    if (not match or int(match[1]) > 2 ** 63 - 1 or not isinstance(cb.message, Message)
            or cb.message.chat.type != "private" or cb.message.chat.id != uid):
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    bot_id = int(match[1])
    row = _owned(ctx, bot_id, uid)
    if row is None:
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    if await state.get_state() is not None:
        await cb.answer(ACTIVE, show_alert=True)
        return
    data = await asyncio.to_thread(read, ctx.sup.bot_dir(bot_id) / "bot.db")
    latest = _owned(ctx, bot_id, uid)
    if latest is None or latest["token"] != row["token"]:
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    text = render(bot_id, latest["username"], data)
    try:
        await cb.message.edit_text(text, reply_markup=ikb([btn("📈 Активность и возвращения", activity_request(bot_id))],
                                                          [btn("🔄 Обновить", f"bst:{bot_id}")],
                                                          [btn("⬅️ К боту", f"b:{bot_id}:open")]))
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            await cb.answer("Не удалось открыть экран. Откройте «Мои боты» заново.", show_alert=True)
            return
    except TelegramAPIError:
        await cb.answer("Не удалось открыть экран. Откройте «Мои боты» заново.", show_alert=True)
        return
    await cb.answer()
