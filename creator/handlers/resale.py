"""🛒 Перепродажа ботов между пользователями (оплата с баланса)."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from child import templates
from child.files import dir_size, human_size
from creator import showcase
from creator.db import InsufficientFunds
from creator.keyboards import BTN_RESALE, btn, ikb
from creator.money import rub
from creator.services import Ctx

router = Router(name="resale")


FILTER_TITLES = {"all": "Все", **{k: templates.info(k).title for k in templates.TEMPLATE_MODULES}}


def market(ctx: Ctx, uid: int, flt: str = "all", number: int = 0):
    rows_db, total, number = showcase.page(ctx.db, uid, flt, number)
    pages = max(1, -(-total // showcase.PAGE))
    fee = ctx.price("RESALE_FEE_PERCENT")
    text = ("🛒 <b>Перепродажа</b>\n\nЗдесь пользователи продают готовых ботов вместе с аудиторией. "
            "Покупка — с баланса, бот сразу переходит к вам.\n"
            f"Комиссия перепродажи {fee}% удерживается из суммы продавца; покупатель платит ровно цену.\n\n"
            f"{FILTER_TITLES[flt]} · Стр. {number + 1}/{pages} · всего {total}")
    rows = [[btn(f"ID {b['id']} | {templates.info(b['template']).title} | {rub(b['sale_price'])}", f"rs:view:{b['id']}")]
            for b in rows_db]
    if not rows_db:
        text += "\n\nСейчас нет ботов на продаже." if flt == "all" else "\n\nВ этом разделе ботов на продаже нет."
    rows.append([btn(("• " if k == flt else "") + t, f"rs:p:{k}:0") for k, t in FILTER_TITLES.items()])
    nav = []
    if number > 0:
        nav.append(btn("⬅️", f"rs:p:{flt}:{number - 1}"))
    if number + 1 < pages:
        nav.append(btn("➡️", f"rs:p:{flt}:{number + 1}"))
    if nav:
        rows.append(nav)
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


@router.callback_query(F.data.startswith("rs:p:"))
async def resale_page(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    if len(parts) != 4 or parts[2] not in FILTER_TITLES or not parts[3].isdigit() or len(parts[3]) > 6:
        return await cb.answer("Кнопка устарела", show_alert=True)
    text, kb = market(ctx, cb.from_user.id, parts[2], int(parts[3]))
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
    raw = cb.data.rsplit(":", 1)[1]
    b = ctx.db.bot(int(raw)) if raw.isdigit() and len(raw) < 12 else None
    if not b or b["sale_price"] is None or b["deleted"]:
        await cb.answer("Бот уже продан", show_alert=True)
        return
    t = templates.info(b["template"])
    price = b["sale_price"]
    own = b["owner_id"] == cb.from_user.id
    text = (f"🤖 <b>Бот ID {b['id']}</b>\n\nСсылка: @{html.escape(b['username'] or '—')}\nШаблон: {t.button}\n"
            f"👥 Пользователей в боте: {child_users(ctx, b['id'])} <i>(записей в базе бота, включая неактивных)</i>\n"
            f"💾 Данные: {human_size(dir_size(ctx.sup.bot_dir(b['id'])))}\n"
            f"💳 Хостинг: {rub(b['hosting_balance'])}\nVIP: {'да' if b['vip'] else 'нет'}\n"
            "<i>Остаток хостинга и VIP остаются у бота после покупки.</i>\n\n"
            f"💰 Цена: <b>{rub(price)}</b>\nВы платите ровно цену; комиссия "
            f"{ctx.price('RESALE_FEE_PERCENT')}% удерживается из суммы продавца.\n")
    rows = []
    if own:
        text += "\nЭто ваш бот — снять с продажи можно в «📤 Продать своего бота»."
    else:
        balance = ctx.db.user(cb.from_user.id)["balance"] if ctx.db.user(cb.from_user.id) else 0
        text += f"\nВаш баланс: {rub(balance)}" + (f" — не хватает {rub(price - balance)}" if balance < price else "")
        rows.append([btn(f"✅ Купить за {rub(price)}", f"rs:buy:{b['id']}")])
    rows.append([btn("⬅️ Назад", "rs:list")])
    await cb.message.edit_text(text, reply_markup=ikb(*rows))
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
    fee = ctx.price("RESALE_FEE_PERCENT")
    example = next((b["sale_price"] for b in bots if b["sale_price"] is not None), 100000)
    await cb.message.edit_text(
        "📤 Выберите бота для продажи (или снятия с продажи):\n\n"
        f"Комиссия перепродажи {fee}% удерживается из цены: при продаже за {rub(example)} "
        f"вы получите {rub(showcase.seller_income(example, fee))}. Хостинг и VIP переходят вместе с ботом.",
        reply_markup=ikb(*rows))
    await cb.answer()
