"""User-facing LavePay withdrawal steps for clicker and refbot.

When platform and per-bot flags are off (the default), this module does not
change the existing free-text manual request. When they are on and the bot
unit is rubles or USDT, the user picks a LavePay method and enters structured
details. Money is reserved in the child DB; the constructor sends the payout.
"""
from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.core import ChildApp, safe_send, wd_kb
from child.db import fmt
from child.payouts import (
    JOB_AWAITING,
    JOB_QUEUED,
    METHOD_LABELS,
    METHOD_PROMPTS,
    dumps_destination,
    format_usdt,
    methods_for_amount,
    normalize_card,
    normalize_phone,
    normalize_sbp_name,
    normalize_trc20,
    payout_unit,
    payouts_auto,
    payouts_ready,
    public_details,
)

BTN_WITHDRAW = "💸 Вывод"


class PayoutWithdraw(StatesGroup):
    method = State()
    card = State()
    sbp_phone = State()
    sbp_bank = State()
    sbp_name = State()
    usdt = State()


def method_keyboard(methods: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=METHOD_LABELS[m], callback_data=f"wdm:{m}")] for m in methods]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="wdm:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payout_keyboard(wid: int, *, can_reject: bool, can_approve: bool) -> InlineKeyboardMarkup | None:
    buttons = []
    if can_approve:
        buttons.append(InlineKeyboardButton(text="✅ Выплатить через LavePay", callback_data=f"wd:ok:{wid}"))
    if can_reject:
        buttons.append(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd:no:{wid}"))
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def build_job(app: ChildApp, method: str, destination: dict[str, Any], amount: float, *, auto: bool) -> dict:
    unit = payout_unit(app.s, app.currency)
    payload: dict[str, Any] = {
        "bot_id": int(app.cfg["bot_id"]),
        "method": method,
        "status": JOB_QUEUED if auto else JOB_AWAITING,
        "destination": dumps_destination(destination),
    }
    if unit == "rub":
        payload["amount_rub"] = int(amount)
    elif unit == "usdt":
        payload["amount_asset"] = format_usdt(amount)
    return payload


async def start_method_choice(message: Message, state: FSMContext, app: ChildApp, amount: float) -> bool:
    """If LavePay is ready, switch the FSM to method selection. Returns True if handled."""
    if not payouts_ready(app):
        return False
    unit = payout_unit(app.s, app.currency)
    methods = methods_for_amount(unit or "", amount)
    if not methods:
        if unit == "rub" and amount != int(amount):
            await message.answer("Для выплаты через LavePay сумма должна быть целыми рублями.")
        else:
            await message.answer(
                "Эта сумма не подходит ни под один способ LavePay "
                "(карта 100–300 000 ₽, СБП 500–100 000 ₽, USDT TRC-20 от 1 USDT)."
            )
        return True
    await state.update_data(amount=amount)
    await state.set_state(PayoutWithdraw.method)
    await message.answer(
        f"💸 Сумма: {fmt(amount)} {app.currency}\nВыберите способ выплаты через LavePay.",
        reply_markup=method_keyboard(methods),
    )
    return True


async def finish_request(message: Message, app: ChildApp, amount: float, destination: dict[str, Any],
                         method: str | None, details: str) -> None:
    from child.withdrawals import format_unit as _unit

    auto = bool(method) and payouts_auto(app)
    payout = build_job(app, method, destination, amount, auto=auto) if method else None
    wid = app.db.create_withdrawal(
        message.from_user.id, amount, details, currency=app.currency, method=method, payout=payout,
    )
    if wid is None:
        await message.answer("Недостаточно средств.")
        return
    if method and auto:
        await message.answer(
            f"✅ Заявка №{wid} принята. Выплата уйдёт через LavePay, когда конструктор её отправит. "
            "Статус — в «👤 Профиль» → «📜 Мои заявки».",
            reply_markup=app.menu(message.from_user.id),
        )
        user = message.from_user
        await safe_send(
            message.bot, app.owner_id,
            f"💸 <b>Автовыплата LavePay, заявка №{wid}</b>\n"
            f"От: {html.escape(user.full_name)} (<code>{user.id}</code>"
            f"{', @' + user.username if user.username else ''})\n"
            f"Сумма: {fmt(amount)} {_unit(app.currency)}\n"
            f"Способ: {html.escape(public_details(method, destination))}\n"
            "Заявка уже в очереди. Отклонить можно, пока она ещё не ушла в LavePay.",
            reply_markup=payout_keyboard(wid, can_reject=True, can_approve=False),
        )
        return
    if method:
        await message.answer(
            f"✅ Заявка №{wid} отправлена владельцу бота. После его подтверждения выплата уйдёт через LavePay. "
            "Статус — в «👤 Профиль» → «📜 Мои заявки».",
            reply_markup=app.menu(message.from_user.id),
        )
        user = message.from_user
        await safe_send(
            message.bot, app.owner_id,
            f"💸 <b>Заявка на вывод №{wid}</b> (LavePay)\n"
            f"От: {html.escape(user.full_name)} (<code>{user.id}</code>"
            f"{', @' + user.username if user.username else ''})\n"
            f"Сумма: {fmt(amount)} {_unit(app.currency)}\n"
            f"Способ: {html.escape(public_details(method, destination))}",
            reply_markup=payout_keyboard(wid, can_reject=True, can_approve=True),
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
    """Owner decision. Returns (action, row) where action is:
    manual_paid, manual_rejected, enqueued, already, in_flight, missing.
    """
    w = app.db.withdrawal(wid)
    if not w:
        return "missing", None
    job = app.db.payout_job(wid)
    if job and job["status"] == "submitted":
        return "in_flight", w
    if job and job["status"] in ("completed", "failed", "cancelled"):
        return "already", w
    if not approve:
        row = app.db.cancel_payout_and_reject(wid) if job else app.db.resolve_withdrawal(wid, False)
        return ("manual_rejected", row) if row else ("already", w)
    if job and job["status"] in ("awaiting_approval", "queued"):
        if not payouts_ready(app):
            return "already", w
        row = app.db.queue_payout_job(wid)
        return ("enqueued", w) if row else ("already", w)
    row = app.db.resolve_withdrawal(wid, True)
    return ("manual_paid", row) if row else ("already", w)


def build_payout_router(app: ChildApp) -> Router:
    r = Router(name="lavepay-payout-flow")

    @r.callback_query(PayoutWithdraw.method, F.data.startswith("wdm:"))
    async def choose_method(cb: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cb.message, Message) or cb.message.chat.type != "private":
            await cb.answer()
            return
        if not payouts_ready(app):
            await state.clear()
            await cb.answer("Выплаты LavePay сейчас выключены.", show_alert=True)
            return
        action = (cb.data or "").split(":", 1)[-1]
        if action == "cancel":
            await state.clear()
            await cb.answer("Отменено.")
            await cb.message.answer("Отменено.", reply_markup=app.menu(cb.from_user.id))
            return
        data = await state.get_data()
        amount = data.get("amount")
        unit = payout_unit(app.s, app.currency)
        methods = methods_for_amount(unit or "", float(amount or 0))
        if action not in methods:
            await cb.answer("Этот способ недоступен для данной суммы.", show_alert=True)
            return
        await state.update_data(method=action)
        if action == "card":
            await state.set_state(PayoutWithdraw.card)
        elif action == "sbp":
            await state.set_state(PayoutWithdraw.sbp_phone)
        else:
            await state.set_state(PayoutWithdraw.usdt)
        await cb.answer()
        await cb.message.answer(METHOD_PROMPTS[action])

    @r.message(PayoutWithdraw.card, F.text)
    async def take_card(message: Message, state: FSMContext) -> None:
        if (message.text or "").strip() == "/cancel":
            return
        try:
            number = normalize_card(message.text or "")
        except ValueError:
            await message.answer("Нужен номер карты из 16–19 цифр.")
            return
        data = await state.get_data()
        await state.clear()
        dest = {"card_number": number}
        await finish_request(message, app, data["amount"], dest, "card", public_details("card", dest))

    @r.message(PayoutWithdraw.sbp_phone, F.text)
    async def take_phone(message: Message, state: FSMContext) -> None:
        if (message.text or "").strip() == "/cancel":
            return
        try:
            phone = normalize_phone(message.text or "")
        except ValueError:
            await message.answer("Введите российский номер, например +79991234567.")
            return
        await state.update_data(phone=phone)
        await state.set_state(PayoutWithdraw.sbp_bank)
        await message.answer("Укажите банк получателя (название, как в СБП, или bank_id).")

    @r.message(PayoutWithdraw.sbp_bank, F.text)
    async def take_bank(message: Message, state: FSMContext) -> None:
        if (message.text or "").strip() == "/cancel":
            return
        bank = " ".join((message.text or "").split())[:80]
        if not bank:
            await message.answer("Укажите банк получателя.")
            return
        await state.update_data(bank=bank)
        await state.set_state(PayoutWithdraw.sbp_name)
        await message.answer("Укажите имя и первую букву фамилии, как в банке: например «Дмитрий Р».")

    @r.message(PayoutWithdraw.sbp_name, F.text)
    async def take_name(message: Message, state: FSMContext) -> None:
        if (message.text or "").strip() == "/cancel":
            return
        try:
            name = normalize_sbp_name(message.text or "")
        except ValueError:
            await message.answer("Нужны имя и одна буква фамилии кириллицей, например «Дмитрий Р».")
            return
        data = await state.get_data()
        await state.clear()
        dest = {"phone": data["phone"], "bank": data["bank"], "recipient_name": name}
        if str(data["bank"]).isdigit():
            dest["bank_id"] = str(data["bank"])
        await finish_request(message, app, data["amount"], dest, "sbp", public_details("sbp", dest))

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
