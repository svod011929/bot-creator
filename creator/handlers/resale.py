"""🛒 Перепродажа ботов между пользователями (оплата с баланса)."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from child import templates
from child.files import dir_size, human_size
from creator.db import InsufficientFunds
from creator.keyboards import BTN_RESALE, btn, ikb
from creator.money import rub
from creator.services import Ctx

router = Router(name="resale")


def market(ctx: Ctx, uid: int):
    bots = ctx.db.bots_for_sale(exclude_owner=uid)
    text = ("🛒 <b>Перепродажа</b>\n\nЗдесь пользователи продают готовых ботов вместе с аудиторией. "
            "Покупка — с баланса, бот сразу переходит к вам.\n")
    rows = [[btn(f"ID {b['id']} | {templates.info(b['template']).title} | {rub(b['sale_price'])}", f"rs:view:{b['id']}")]
            for b in bots]
    if not bots:
        text += "\nСейчас нет ботов на продаже."
    rows.append([btn("📤 Продать своего бота", "rs:mine")])
    return text, ikb(*rows)


@router.message(F.text == BTN_RESALE)
async def resale(message: Message, ctx: Ctx) -> None:
    text, kb = market(ctx, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "rs:list")
async def resale_cb(cb: CallbackQuery, ctx: Ctx) -> None:
    text, kb = market(ctx, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


def child_users(ctx: Ctx, bot_id: int) -> int:
    import sqlite3

    path = ctx.sup.bot_dir(bot_id) / "bot.db"
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    except sqlite3.Error:
        return 0


@router.callback_query(F.data.startswith("rs:view:"))
async def view(cb: CallbackQuery, ctx: Ctx) -> None:
    b = ctx.db.bot(int(cb.data.rsplit(":", 1)[1]))
    if not b or b["sale_price"] is None:
        await cb.answer("Бот уже продан", show_alert=True)
        return
    t = templates.info(b["template"])
    await cb.message.edit_text(
        f"🤖 <b>Бот ID {b['id']}</b>\n\nСсылка: @{html.escape(b['username'] or '—')}\nШаблон: {t.button}\n"
        f"👥 Пользователей в боте: {child_users(ctx, b['id'])}\n"
        f"💾 Данные: {human_size(dir_size(ctx.sup.bot_dir(b['id'])))}\n"
        f"💳 Хостинг: {rub(b['hosting_balance'])}\nVIP: {'да' if b['vip'] else 'нет'}\n\n"
        f"💰 Цена: <b>{rub(b['sale_price'])}</b>",
        reply_markup=ikb([btn(f"✅ Купить за {rub(b['sale_price'])}", f"rs:buy:{b['id']}")], [btn("⬅️ Назад", "rs:list")]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rs:buy:"))
async def buy(cb: CallbackQuery, ctx: Ctx) -> None:
    bot_id = int(cb.data.rsplit(":", 1)[1])
    try:
        seller, price, income = ctx.db.buy_bot(cb.from_user.id, bot_id, ctx.price("RESALE_FEE_PERCENT"))
    except InsufficientFunds:
        await cb.answer("Недостаточно средств на балансе. Пополните в «💼 Мой кабинет».", show_alert=True)
        return
    except LookupError:
        await cb.answer("Бот уже продан или снят с продажи", show_alert=True)
        return
    await ctx.apply_changes(bot_id)
    await cb.message.edit_text(f"🎉 Вы купили бота ID {bot_id} за {rub(price)}! Он в разделе «🤖 Мои боты».")
    await cb.answer()
    try:
        await cb.bot.send_message(seller, f"💰 Ваш бот ID {bot_id} продан! На баланс зачислено {rub(income)}.")
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data == "rs:mine")
async def mine(cb: CallbackQuery, ctx: Ctx) -> None:
    bots = ctx.db.user_bots(cb.from_user.id)
    if not bots:
        await cb.answer("У вас нет ботов", show_alert=True)
        return
    rows = [[btn(f"ID {b['id']} | {templates.info(b['template']).title}"
                 + (f" | продаётся {rub(b['sale_price'])}" if b["sale_price"] is not None else ""),
                 f"b:{b['id']}:{'unsell' if b['sale_price'] is not None else 'sell'}")] for b in bots]
    rows.append([btn("⬅️ Назад", "rs:list")])
    await cb.message.edit_text("📤 Выберите бота для продажи (или снятия с продажи):", reply_markup=ikb(*rows))
    await cb.answer()
