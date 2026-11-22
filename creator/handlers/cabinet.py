"""💼 Мой кабинет и 👥 Партнёры."""
from __future__ import annotations

import html
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from creator import ops_history
from creator.keyboards import BTN_CABINET, BTN_PARTNERS, btn, ikb
from creator.money import rub
from creator.services import Ctx

router = Router(name="cabinet")

KIND_TEXT = {
    "topup": "Пополнение", "create": "Создание бота", "hosting": "Хостинг", "vip": "VIP", "ref": "Партнёрские",
    "resale_buy": "Покупка бота", "resale_sell": "Продажа бота", "refund": "Возврат", "admin": "Корректировка",
    "tgrass": "TGrass ОП", "piarflow": "PiarFlow задания",
}


def cabinet_text(ctx: Ctx, uid: int) -> str:
    u = ctx.db.user(uid)
    bots = ctx.db.user_bots(uid)
    return (
        "💼 <b>Мой кабинет</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📅 С нами с: {time.strftime('%d.%m.%Y', time.localtime(u['created_at']))}\n"
        f"💰 Баланс: <b>{rub(u['balance'])}</b>\n"
        f"🤖 Ботов: {len(bots)} (запущено {sum(1 for b in bots if b['status'] == 'running')})\n"
        f"👥 Рефералов: {ctx.db.referrals_count(uid)}, заработано {rub(u['ref_earned'])}"
    )


def cabinet_kb():
    return ikb([btn("➕ Пополнить баланс", "cab:topup")], [btn("📜 История операций", "cab:history")])


@router.message(F.text == BTN_CABINET)
async def cabinet(message: Message, ctx: Ctx) -> None:
    await message.answer(cabinet_text(ctx, message.from_user.id), reply_markup=cabinet_kb())


@router.callback_query(F.data == "cab:main")
async def cabinet_cb(cb: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text(cabinet_text(ctx, cb.from_user.id), reply_markup=cabinet_kb())
    await cb.answer()


def _private(cb: CallbackQuery) -> bool:
    return isinstance(cb.message, Message) and cb.message.chat.type == "private" and cb.message.chat.id == cb.from_user.id


def kind_text(kind: str) -> str:
    return html.escape(KIND_TEXT.get(kind, kind))


def signed(amount: int) -> str:
    return ("+" if amount > 0 else "") + rub(amount)


async def show_history(cb: CallbackQuery, ctx: Ctx, flt: str, number: int) -> None:
    rows, total, number = ops_history.page(ctx.db, cb.from_user.id, flt, number)
    pages = max(1, -(-total // ops_history.PAGE))
    title = ops_history.FILTERS[flt][0]
    lines = [f"📜 <b>История операций</b> · {title}", f"Стр. {number + 1}/{pages} · всего {total}\n"]
    for r in rows:
        lines.append(f"№{r['id']} · {ops_history.when(r['created_at'])[:10]} | {signed(r['amount'])} | {kind_text(r['kind'])}")
    if not rows:
        lines.append("Операций пока нет." if flt == "all" else "В этом разделе операций нет.")
    lines.append("\nНажмите номер, чтобы открыть операцию. Суммы — в рублях баланса конструктора.")
    filters = [btn(("• " if key == flt else "") + name, f"cab:h:{key}:0") for key, (name, _) in ops_history.FILTERS.items()]
    numbers = [btn(f"№{r['id']}", f"cab:tx:{r['id']}:{flt}:{number}") for r in rows]
    nav = []
    if number > 0:
        nav.append(btn("⬅️", f"cab:h:{flt}:{number - 1}"))
    if number + 1 < pages:
        nav.append(btn("➡️", f"cab:h:{flt}:{number + 1}"))
    await cb.message.edit_text("\n".join(lines), reply_markup=ikb(
        filters[:3], filters[3:], numbers[:5], numbers[5:], nav, [btn("⬅️ В кабинет", "cab:main")]))


@router.callback_query(F.data == "cab:history")
async def history(cb: CallbackQuery, ctx: Ctx) -> None:
    if not _private(cb):
        await cb.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    await cb.answer()
    await show_history(cb, ctx, "all", 0)


@router.callback_query(F.data.startswith("cab:h:") | F.data.startswith("cab:tx:"))
async def history_nav(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = (cb.data or "").split(":")
    try:
        if parts[1] == "h" and len(parts) == 4:
            flt, number, tx_id = parts[2], int(parts[3]), None
        elif parts[1] == "tx" and len(parts) == 5:
            tx_id, flt, number = int(parts[2]), parts[3], int(parts[4])
        else:
            raise ValueError
        if flt not in ops_history.FILTERS or not 0 <= number < 10**6 or (tx_id is not None and tx_id <= 0):
            raise ValueError
    except (ValueError, IndexError):
        await cb.answer("Кнопка устарела — откройте историю заново.", show_alert=True)
        return
    if not _private(cb):
        await cb.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    if tx_id is None:
        await cb.answer()
        await show_history(cb, ctx, flt, number)
        return
    r = ops_history.operation(ctx.db, cb.from_user.id, tx_id)
    if r is None:
        await cb.answer("Операция недоступна.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"🧾 <b>Операция №{r['id']}</b>\n\nДата: {ops_history.when(r['created_at'])}\n"
        f"Сумма: <b>{signed(r['amount'])}</b>\nТип: {kind_text(r['kind'])}\n"
        f"Описание: {ops_history.description(r['kind'], r['comment'])}\n\n"
        "Если с операцией что-то не так — сообщите через «⛔ Оповестить о проблеме», указав её номер.",
        reply_markup=ikb([btn("⛔ Оповестить о проблеме", "info:report")],
                         [btn("⬅️ К списку", f"cab:h:{flt}:{number}")], [btn("⬅️ В кабинет", "cab:main")]))


@router.message(F.text == BTN_PARTNERS)
async def partners(message: Message, ctx: Ctx) -> None:
    uid = message.from_user.id
    u = ctx.db.user(uid)
    link = f"https://t.me/{ctx.bot_username}?start=ref{uid}"
    await message.answer(
        "👥 <b>Партнёрская программа</b>\n\n"
        f"Приглашайте пользователей и получайте <b>{ctx.price('REF_PERCENT')}%</b> с каждой их оплаты "
        "— навсегда. Начисления приходят на баланс, им можно оплачивать ботов, "
        "хостинг и VIP.\n\n"
        f"👤 Приглашено: {ctx.db.referrals_count(uid)}\n"
        f"💰 Заработано: {rub(u['ref_earned'])}\n\n"
        f"🔗 Ваша ссылка:\n<code>{link}</code>",
        disable_web_page_preview=True,
    )
