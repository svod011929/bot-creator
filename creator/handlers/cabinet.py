"""💼 Мой кабинет и 👥 Партнёры."""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

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


@router.callback_query(F.data == "cab:history")
async def history(cb: CallbackQuery, ctx: Ctx) -> None:
    rows = ctx.db.history(cb.from_user.id)
    lines = ["📜 <b>Последние операции</b>\n"]
    for r in rows:
        sign = "+" if r["amount"] > 0 else ""
        when = time.strftime("%d.%m %H:%M", time.localtime(r["created_at"]))
        lines.append(f"{when} | {sign}{rub(r['amount'])} | {KIND_TEXT.get(r['kind'], r['kind'])}")
    if not rows:
        lines.append("Операций пока нет.")
    await cb.message.edit_text("\n".join(lines), reply_markup=ikb([btn("⬅️ Назад", "cab:main")]))
    await cb.answer()


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
