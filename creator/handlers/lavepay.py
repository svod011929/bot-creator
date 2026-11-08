"""Оплата через LavePay (₽): счёт со ссылкой, «Проверить оплату»."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from child import templates
from creator.keyboards import btn, cancel_kb, ikb, url_btn
from creator.lavepay import MAX_TOPUP_RUB, MIN_TOPUP_RUB, LavePayError
from creator.money import rub
from creator.services import Ctx
from creator.states import Cabinet

router = Router(name="lavepay")
log = logging.getLogger("lavepay.ui")

RUB_TOPUP_OPTIONS = (100, 250, 500, 1000, 2500)
STATUS_TEXT = {"expired": "⌛ Счёт истёк — создайте новый.", "cancelled": "Счёт отменён.",
               "refunded": "Платёж возвращён.", "mismatch": "⚠️ Сумма оплаты не совпала, напишите администратору."}


def lave_on(ctx: Ctx) -> bool:
    return ctx.lave is not None and ctx.lave.enabled


def build_payload(ctx: Ctx, uid: int, data: str) -> tuple[str, str] | None:
    """callback lp:… -> (payload, описание). Все суммы считаются на сервере."""
    from creator.handlers.mybots import HOSTING_OPTIONS
    parts = data.split(":")[1:]
    kind = parts[0] if parts else ""
    try:
        if kind == "t":
            rub = int(parts[1])
            p = ctx.lave.topup_payload(rub)
            return (p, "Пополнение баланса конструктора") if p else None
        if kind == "c":
            d = ctx.db.draft(int(parts[1]))
            if not d or d["used"] or d["user_id"] != uid:
                return None
            t = templates.info(d["template"])
            return f"create:{d['id']}", f"Бот @{d['bot_username']} по шаблону «{t.title}»"
        if kind == "h":
            bot_id, days = int(parts[1]), int(parts[2])
            if days not in HOSTING_OPTIONS:
                return None
            amount = ctx.hosting_pack_price(days)
            return (f"host:{bot_id}:{amount}", f"Хостинг бота ID {bot_id} на {days} дн.") if amount > 0 else None
        if kind == "v":
            bot_id = int(parts[1])
            return f"vip:{bot_id}", f"VIP для бота ID {bot_id}"
    except (ValueError, IndexError):
        return None
    return None


def invoice_kb(row):
    return ikb([url_btn(f"💳 Оплатить {rub(row['amount'])} (${row['usd']})", row["pay_url"])],
               [btn("🔄 Проверить оплату", f"lpc:{row['id']}")])


async def send_lave_invoice(target: Message, ctx: Ctx, uid: int, payload: str, description: str) -> str | None:
    """Создаёт счёт и отправляет сообщение с кнопками. Возвращает текст ошибки или None."""
    try:
        row = await ctx.lave.create(uid, payload, description)
    except ValueError as e:
        return str(e)
    except LavePayError as e:
        log.warning("create invoice failed: %s", e)
        return "LavePay сейчас недоступен, попробуйте позже или оплатите с баланса."
    except Exception:  # noqa: BLE001
        log.exception("create invoice failed")
        return "LavePay сейчас недоступен, попробуйте позже или оплатите с баланса."
    await target.bot.send_message(
        uid,
        f"🧾 <b>Счёт LavePay</b>\n\n{html.escape(description)}\n"
        f"💰 К оплате: <b>{rub(row['amount'])}</b> (выставлено ${row['usd']}; оплата балансом Laveshka, "
        "USDT/криптой, CryptoBot или xRocket)\n\n"
        "Нажмите «Оплатить» — откроется Laveshka. Счёт действует 1 час. "
        "Зачисление автоматическое; если не пришло — «🔄 Проверить оплату».",
        reply_markup=invoice_kb(row),
    )
    return None


@router.callback_query(F.data.startswith("lp:"))
async def lave_pay(cb: CallbackQuery, ctx: Ctx) -> None:
    if not lave_on(ctx):
        await cb.answer("LavePay пока не подключён", show_alert=True)
        return
    built = build_payload(ctx, cb.from_user.id, cb.data)
    if not built:
        await cb.answer("Заказ устарел или неверная сумма", show_alert=True)
        return
    err = await send_lave_invoice(cb.message, ctx, cb.from_user.id, *built)
    await cb.answer(err or "Счёт создан 👇", show_alert=bool(err))


@router.callback_query(F.data.startswith("lpc:"))
async def lave_check(cb: CallbackQuery, ctx: Ctx) -> None:
    if not lave_on(ctx):
        await cb.answer("LavePay пока не подключён", show_alert=True)
        return
    local_id = int(cb.data.split(":")[1])
    row = ctx.db.lave_get(local_id)
    if not row or row["user_id"] != cb.from_user.id:
        await cb.answer("Счёт не найден", show_alert=True)
        return
    try:
        status, _ = await ctx.lave.check(local_id)
    except Exception as e:  # noqa: BLE001
        log.warning("check %s failed: %s", local_id, e)
        await cb.answer("Не удалось проверить, попробуйте через минуту", show_alert=True)
        return
    if status == "paid":
        await cb.answer("✅ Оплата получена и зачислена", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
    elif status == "active":
        await cb.answer("⏳ Оплата пока не поступила. Если уже оплатили — подождите минуту.", show_alert=True)
    else:
        await cb.answer(STATUS_TEXT.get(status, f"Статус счёта: {status}"), show_alert=True)


# ---------- пополнение баланса в рублях ----------
@router.callback_query(F.data.in_({"cab:lave", "cab:topup"}))
async def lave_topup_menu(cb: CallbackQuery, ctx: Ctx) -> None:
    if not lave_on(ctx):
        await cb.answer("LavePay пока не подключён", show_alert=True)
        return
    rows = [[btn(f"{r} ₽", f"lp:t:{r}") for r in RUB_TOPUP_OPTIONS[:3]],
            [btn(f"{r} ₽", f"lp:t:{r}") for r in RUB_TOPUP_OPTIONS[3:]],
            [btn("✏️ Другая сумма", "cab:lavecustom")], [btn("⬅️ Назад", "cab:main")]]
    await cb.message.edit_text(
        "➕ <b>Пополнение баланса</b>\n\nОплата через LavePay (балансом Laveshka, USDT/криптой, CryptoBot "
        "или xRocket). Выберите сумму в рублях:",
        reply_markup=ikb(*rows))
    await cb.answer()


@router.callback_query(F.data == "cab:lavecustom")
async def lave_topup_custom(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Cabinet.lave_amount)
    await cb.message.edit_text(f"✏️ Введите сумму пополнения в ₽ (от {MIN_TOPUP_RUB} до {MAX_TOPUP_RUB}):",
                               reply_markup=cancel_kb("cab:main"))
    await cb.answer()


@router.message(Cabinet.lave_amount, F.text)
async def lave_topup_amount(message: Message, state: FSMContext, ctx: Ctx) -> None:
    raw = message.text.strip()
    if not raw.isdigit() or not MIN_TOPUP_RUB <= int(raw) <= MAX_TOPUP_RUB:
        await message.answer(f"Введите целое число от {MIN_TOPUP_RUB} до {MAX_TOPUP_RUB}.")
        return
    await state.clear()
    if not lave_on(ctx):
        await message.answer("LavePay пока не подключён")
        return
    payload = ctx.lave.topup_payload(int(raw))
    if not payload:
        await message.answer("Слишком маленькая сумма.")
        return
    err = await send_lave_invoice(message, ctx, message.from_user.id, payload, "Пополнение баланса конструктора")
    if err:
        await message.answer(err)
