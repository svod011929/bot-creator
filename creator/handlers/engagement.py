"""Private owner activity reports; read-only queries run outside the event loop."""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from creator.content import ContentError, _guard
from creator.engagement import parse, read_owned, render
from creator.services import Ctx

router = Router(name="engagement-reports")


@router.callback_query(F.data.startswith("eng:"))
async def show(cb: CallbackQuery, state: FSMContext, ctx: Ctx):
    if not isinstance(cb.message, Message) or cb.message.chat.type != "private" or cb.message.chat.id != cb.from_user.id:
        await cb.answer("Откройте аналитику в личном чате с конструктором.", show_alert=True)
        return
    try:
        bot_id, section, days = parse(cb.data)
        before = await state.get_state(), await state.get_data()
        if before[0] is not None:
            raise ContentError("Завершите текущую настройку или нажмите /cancel.")
        data, identity = await asyncio.to_thread(read_owned, ctx, cb.from_user.id, bot_id, days=days)
        if (await state.get_state(), await state.get_data()) != before:
            raise ContentError("Откройте аналитику заново после текущей настройки.")
        with ctx.db.owned_bot(bot_id, cb.from_user.id) as row:
            if (row["owner_id"], row["template"], row["token"], row["username"]) != identity:
                raise ContentError("Бот изменился. Откройте его карточку заново.")
            _guard(ctx, row, cb.from_user.id)
        text, keyboard = render(bot_id, data, section=section, days=days)
        try:
            await cb.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                raise ContentError("Экран не отправлен. Откройте аналитику заново.") from None
        except TelegramAPIError:
            raise ContentError("Экран не отправлен. Откройте аналитику заново.") from None
        await cb.answer()
    except (ContentError, ValueError):
        # Known presentation errors contain no raw SQLite/provider values.
        await cb.answer(
            "Аналитика недоступна или действие устарело. Завершите настройку и откройте карточку своего бота.", show_alert=True
        )
