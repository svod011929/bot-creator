"""User-facing LavePay withdrawal steps for clicker and refbot (USDT TRC-20).

When the constructor has not connected the owner's shop, this module does not
change the existing free-text manual request. When the path is ready, the user
enters a TRC-20 address; the owner confirms the USDT quote in the constructor.
"""
from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.core import ChildApp, safe_send, wd_kb
from child.db import fmt
from child.money import from_minor
from child.payouts import (
    JOB_AWAITING,
    dumps_destination,
    format_usdt,
    normalize_trc20,
    payouts_ready,
    public_details,
)


class PayoutWithdraw(StatesGroup):
    usdt = State()


def constructor_link(app: ChildApp, wid: int) -> str | None:
    username = str(app.cfg.get("creator_username") or "")
    bot_id = app.cfg.get("bot_id")
    if not username or not isinstance(bot_id, int) or bot_id <= 0:
        return None
    return f"https://t.me/{username}?start=lpayout_{bot_id}_{wid}"


def payout_keyboard(wid: int, app: ChildApp, *, can_reject: bool) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    link = constructor_link(app, wid)
    if link:
        rows.append([InlineKeyboardButton(text="✅ Подтвердить в конструкторе", url=link)])
    if can_reject:
        rows.append([InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd:no:{wid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def build_job(app: ChildApp, destination: dict[str, Any], amount: float) -> dict:
    cfg = app.cfg if isinstance(app.cfg, dict) else {}
    payload: dict[str, Any] = {
        "bot_id": int(cfg.get("bot_id") or 0),
        "method": "usdt_trc20",
        "status": JOB_AWAITING,
        "destination": dumps_destination(destination),
    }
    kind = cfg.get("lavepay_unit_kind")
    rate = cfg.get("lavepay_unit_rate")
    if kind == "usdt" and rate:
        try:
            payload["amount_asset"] = format_usdt(float(from_minor(amount)) * float(rate))
        except (TypeError, ValueError):
            pass
    elif kind == "rub" and rate:
        try:
            payload["amount_rub"] = int(float(from_minor(amount)) * float(rate))
        except (TypeError, ValueError):
            pass
    return payload


async def start_method_choice(message: Message, state: FSMContext, app: ChildApp, amount: float) -> bool:
    """If LavePay is ready, collect a TRC-20 address. Returns True if handled."""
    if not payouts_ready(app):
        return False
    await state.update_data(amount=amount)
    await state.set_state(PayoutWithdraw.usdt)
    await message.answer(
        f"💸 Сумма: {fmt(amount)} {app.currency}\n"
        "Выплата уйдёт в USDT (сеть TRC-20) с магазина LavePay владельца бота, "
        "после его подтверждения в конструкторе.\n\n"
        "Введите адрес USDT в сети TRC-20 (начинается с T).",
    )
    return True


async def finish_request(message: Message, app: ChildApp, amount: float, destination: dict[str, Any],
                         method: str | None, details: str) -> None:
    from child.withdrawals import format_unit as _unit

    payout = build_job(app, destination, amount) if method else None
    wid = app.db.create_withdrawal(
        message.from_user.id, amount, details, currency=app.currency, method=method, payout=payout,
    )
    if wid is None:
        await message.answer("Недостаточно средств.")
        return
    if method:
        await message.answer(
            f"✅ Заявка №{wid} отправлена владельцу бота. После подтверждения в конструкторе "
            "выплата уйдёт в USDT TRC-20. Статус — в «👤 Профиль» → «📜 Мои заявки».",
            reply_markup=app.menu(message.from_user.id),
        )
        user = message.from_user
        link = constructor_link(app, wid)
        extra = f"\nПодтверждение: {link}" if link else "\nОткройте конструктор, карточку бота → «LavePay выплаты»."
        await safe_send(
            message.bot, app.owner_id,
            f"💸 <b>Заявка на вывод №{wid}</b> (LavePay, USDT TRC-20)\n"
            f"От: {html.escape(user.full_name)} (<code>{user.id}</code>"
            f"{', @' + user.username if user.username else ''})\n"
            f"Сумма: {fmt(amount)} {_unit(app.currency)}\n"
            f"Адрес: {html.escape(public_details(method, destination))}\n"
            "Курс USDT будет показан в конструкторе перед подтверждением."
            f"{extra}",
            reply_markup=payout_keyboard(wid, app, can_reject=True),
        )
        return
    await message.answer(
        f"✅ Заявка №{wid} отправлена владельцу бота. Выплату владелец проводит вручную; "
        "статус заявки — в «👤 Профиль» → «📜 Мои заявки».",
        reply_markup=app.menu(message.from_user.id),
    )
    user = message.from_user
    await safe_send(
        message.bot, app.owner_id,
        f"💸 <b>Заявка на вывод №{wid}</b>\nОт: {html.escape(user.full_name)} (<code>{user.id}</code>"
        f"{', @' + user.username if user.username else ''})\nСумма: {fmt(amount)} {_unit(app.currency)}\n"
        f"Реквизиты: <code>{html.escape(details)}</code>",
        reply_markup=wd_kb(wid),
    )


def owner_resolve(app: ChildApp, wid: int, approve: bool) -> tuple[str, Any]:
    """Owner decision in the child bot.

    LavePay approval happens in the constructor (live USDT quote). Here the
    owner may only reject a request that has not yet been submitted, or use
    the existing manual mark when there is no payout job.
    """
    w = app.db.withdrawal(wid)
    if not w:
        return "missing", None
    job = app.db.payout_job(wid)
    if job and job["status"] == "submitted":
        return "in_flight", w
    if job and job["status"] in ("completed", "failed", "cancelled"):
        return "already", w
    if job and approve:
        return "confirm_in_creator", w
    if not approve:
        row = app.db.cancel_payout_and_reject(wid) if job else app.db.resolve_withdrawal(wid, False)
        return ("manual_rejected", row) if row else ("already", w)
    row = app.db.resolve_withdrawal(wid, True)
    return ("manual_paid", row) if row else ("already", w)


def build_payout_router(app: ChildApp) -> Router:
    r = Router(name="lavepay-payout-flow")

    @r.message(PayoutWithdraw.usdt, F.text)
    async def take_usdt(message: Message, state: FSMContext) -> None:
        if (message.text or "").strip() == "/cancel":
            return
        try:
            address = normalize_trc20(message.text or "")
        except ValueError:
            await message.answer("Нужен адрес USDT в сети TRC-20 (34 символа, начинается с T).")
            return
        data = await state.get_data()
        await state.clear()
        dest = {"address": address}
        await finish_request(message, app, data["amount"], dest, "usdt_trc20", public_details("usdt_trc20", dest))

    return r
