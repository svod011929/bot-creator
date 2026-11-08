"""Админ-панель конструктора (только для ADMIN_IDS)."""
from __future__ import annotations

import asyncio
import html

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from child import templates
from creator.config import MONEY_KEYS, PRICE_KEYS
from creator.keyboards import STATUS_TEXT, btn, cancel_kb, ikb, status_icon
from creator.money import parse_rub, rub
from creator.services import Ctx
from creator.states import Admin

router = Router(name="admin")


class IsAdmin(Filter):
    async def __call__(self, event, ctx: Ctx) -> bool:
        return ctx.is_admin(event.from_user.id)


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def panel_kb(ctx: Ctx | None = None):
    row = [btn("🤖 Боты", "a:bots")]
    if ctx is not None and ctx.tgrass is not None:
        row.append(btn("💸 TGrass", "a:tg"))
    if ctx is not None and ctx.piarflow is not None:
        row.append(btn("🚀 PiarFlow", "a:pf"))
    return ikb([btn("📊 Статистика", "a:stats"), btn("💰 Цены", "a:prices")],
               [btn("📢 Рассылка", "a:bc"), btn("👤 Пользователи", "a:users")],
               row)


@router.message(Command("admin"))
async def panel(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await message.answer("🛠 <b>Админ-панель</b>\n\nОтветить пользователю: <code>/reply ID текст</code>",
                         reply_markup=panel_kb(ctx))


@router.callback_query(F.data == "a:main")
async def panel_cb(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await cb.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=panel_kb(ctx))
    await cb.answer()


@router.message(Command("reply"))
async def reply(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split(maxsplit=1)
    if len(args) < 2 or not args[0].isdigit():
        await message.answer("Формат: /reply ID текст")
        return
    try:
        await message.bot.send_message(int(args[0]), f"📩 <b>Ответ администратора:</b>\n\n{html.escape(args[1])}")
        await message.answer("✅ Отправлено")
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ Не доставлено: {html.escape(str(e))}")


@router.callback_query(F.data == "a:stats")
async def stats(cb: CallbackQuery, ctx: Ctx) -> None:
    s = ctx.db.stats()
    alive = sum(1 for i in ctx.sup.children if ctx.sup.is_running(i))
    await cb.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: {s['users_total']} (сегодня +{s['users_today']})\n"
        f"🤖 Ботов: {s['bots_total']}, запущено: {s['bots_running']} (процессов: {alive})\n"
        f"💳 Оплат всего: {rub(s['payments_total'])}, сегодня: {rub(s['payments_today'])}\n"
        f"💼 Сумма балансов пользователей: {rub(s['balances'])}",
        reply_markup=ikb([btn("⬅️ Назад", "a:main")]),
    )
    await cb.answer()


def show_price(key: str, value: int) -> str:
    return rub(value) if key in MONEY_KEYS else str(value)


@router.callback_query(F.data == "a:prices")
async def prices(cb: CallbackQuery, ctx: Ctx) -> None:
    rows = [[btn(f"{label}: {show_price(key, ctx.price(key))}", f"a:price:{key}")] for key, label in PRICE_KEYS.items()
            if (ctx.tgrass is not None or not key.startswith("TGRASS_"))
            and (ctx.piarflow is not None or not key.startswith("PIARFLOW_"))]
    rows.append([btn("⬅️ Назад", "a:main")])
    await cb.message.edit_text("💰 <b>Цены</b> (нажмите, чтобы изменить)", reply_markup=ikb(*rows))
    await cb.answer()


@router.callback_query(F.data.startswith("a:price:"))
async def price_pick(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":", 2)[2]
    if key not in PRICE_KEYS:
        await cb.answer()
        return
    await state.set_state(Admin.price)
    await state.update_data(key=key)
    hint = "сумма в ₽, можно с копейками: 225 или 4,50" if key in MONEY_KEYS else "целое число ≥ 0"
    await cb.message.edit_text(f"Введите новое значение «{PRICE_KEYS[key]}» ({hint}):",
                               reply_markup=cancel_kb("a:prices"))
    await cb.answer()


@router.message(Admin.price, F.text)
async def price_set(message: Message, state: FSMContext, ctx: Ctx) -> None:
    raw = message.text.strip()
    key = (await state.get_data())["key"]
    if key in MONEY_KEYS:
        value = parse_rub(raw)
        if value is None:
            await message.answer("Введите сумму в ₽, например 225 или 4,50.")
            return
    else:
        if not raw.isdigit():
            await message.answer("Введите целое число ≥ 0.")
            return
        value = int(raw)
    if key == "LAVE_USD_RUB" and value < 1:
        await message.answer("Значение должно быть ≥ 1.")
        return
    await state.clear()
    ctx.db.kv_set(f"price:{key}", str(value))
    await message.answer(f"✅ {PRICE_KEYS[key]}: {show_price(key, value)}",
                         reply_markup=ikb([btn("💰 К ценам", "a:prices")]))


@router.callback_query(F.data == "a:bc")
async def bc(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.broadcast)
    await cb.message.edit_text("📢 Пришлите сообщение для рассылки всем пользователям конструктора.",
                               reply_markup=cancel_kb("a:main"))
    await cb.answer()


@router.message(Admin.broadcast)
async def bc_send(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    ids = ctx.db.all_user_ids()
    await message.answer(f"🚀 Рассылка запущена: {len(ids)} получателей.")

    async def run() -> None:
        ok = fail = 0
        for uid in ids:
            try:
                await message.copy_to(uid)
                ok += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                fail += 1
            except TelegramForbiddenError:
                fail += 1
            except Exception:  # noqa: BLE001
                fail += 1
            await asyncio.sleep(0.05)
        await message.answer(f"✅ Рассылка завершена: доставлено {ok}, ошибок {fail}.")

    asyncio.create_task(run())


@router.callback_query(F.data == "a:users")
async def users(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.user_lookup)
    await cb.message.edit_text("👤 Пришлите ID пользователя:", reply_markup=cancel_kb("a:main"))
    await cb.answer()


def user_card(ctx: Ctx, uid: int):
    u = ctx.db.user(uid)
    if not u:
        return None, None
    bots = ctx.db.user_bots(uid)
    text = (f"👤 <b>{html.escape(u['full_name'] or '')}</b> {('@' + u['username']) if u['username'] else ''}\n"
            f"ID: <code>{uid}</code>\nБаланс: {rub(u['balance'])}\nРефералов: {ctx.db.referrals_count(uid)}\n"
            f"Ботов: {len(bots)} ({', '.join(str(b['id']) for b in bots) or '—'})\n"
            f"Статус: {'🚫 забанен' if u['banned'] else '✅ активен'}")
    kb = ikb([btn("💰 Изменить баланс", f"a:ubal:{uid}")],
             [btn("✅ Разбанить" if u["banned"] else "🚫 Забанить", f"a:uban:{uid}")],
             [btn("⬅️ Назад", "a:main")])
    return text, kb


@router.message(Admin.user_lookup, F.text)
async def user_lookup(message: Message, state: FSMContext, ctx: Ctx) -> None:
    raw = message.text.strip().lstrip("@")
    uid = int(raw) if raw.isdigit() else ctx.db.scalar("SELECT id FROM users WHERE username=?", raw)
    text, kb = user_card(ctx, uid or 0)
    if not text:
        await message.answer("Пользователь не найден.")
        return
    await state.clear()
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("a:uban:"))
async def user_ban(cb: CallbackQuery, ctx: Ctx) -> None:
    uid = int(cb.data.rsplit(":", 1)[1])
    u = ctx.db.user(uid)
    if u:
        ctx.db.set_banned(uid, not u["banned"])
        text, kb = user_card(ctx, uid)
        await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("a:ubal:"))
async def user_bal(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.user_balance)
    await state.update_data(uid=int(cb.data.rsplit(":", 1)[1]))
    await cb.message.answer("Введите изменение баланса в ₽ (например 100, -50 или 12,50):", reply_markup=cancel_kb("a:main"))
    await cb.answer()


@router.message(Admin.user_balance, F.text)
async def user_bal_set(message: Message, state: FSMContext, ctx: Ctx) -> None:
    delta = parse_rub(message.text, allow_negative=True)
    if delta is None:
        await message.answer("Введите сумму в ₽, например 100, -50 или 12,50.")
        return
    uid = (await state.get_data())["uid"]
    await state.clear()
    new = ctx.db.change_balance(uid, delta, "admin", f"Администратор {message.from_user.id}", allow_negative=True)
    await message.answer(f"✅ Баланс <code>{uid}</code>: {rub(new)}")


@router.callback_query(F.data == "a:bots")
async def bots(cb: CallbackQuery, ctx: Ctx) -> None:
    rows = ctx.db.all("SELECT * FROM bots WHERE deleted=0 ORDER BY id DESC LIMIT 30")
    kb = [[btn(f"ID {b['id']} {status_icon(b['status'])} @{b['username']} | {templates.info(b['template']).title}",
               f"a:bot:{b['id']}")] for b in rows]
    kb.append([btn("⬅️ Назад", "a:main")])
    await cb.message.edit_text(f"🤖 <b>Боты</b> (последние {len(rows)})", reply_markup=ikb(*kb))
    await cb.answer()


@router.callback_query(F.data.startswith("a:bot:"))
async def bot_card(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    bot_id = int(parts[2])
    action = parts[3] if len(parts) > 3 else ""
    if action == "start":
        await ctx.sup.start(bot_id)
    elif action == "stop":
        await ctx.sup.stop(bot_id)
    elif action == "del":
        await ctx.delete_bot(bot_id)
        await cb.message.edit_text(f"🗑 Бот ID {bot_id} удалён.", reply_markup=ikb([btn("⬅️ Боты", "a:bots")]))
        await cb.answer()
        return
    b = ctx.db.bot(bot_id)
    if not b:
        await cb.answer("Не найден", show_alert=True)
        return
    await cb.message.edit_text(
        f"🤖 <b>Бот ID {b['id']}</b> @{b['username']}\nВладелец: <code>{b['owner_id']}</code>\n"
        f"Шаблон: {templates.info(b['template']).title}\nСтатус: {STATUS_TEXT.get(b['status'], b['status'])}\n"
        f"Хостинг: {rub(b['hosting_balance'])}\nVIP: {'да' if b['vip'] else 'нет'}\n"
        f"Ошибка: {html.escape(b['last_error'] or '—')}",
        reply_markup=ikb([btn("▶️ Запустить", f"a:bot:{bot_id}:start"), btn("⏹ Остановить", f"a:bot:{bot_id}:stop")],
                         [btn("🗑 Удалить", f"a:bot:{bot_id}:delask")] if action != "delask"
                         else [btn("⚠️ Точно удалить", f"a:bot:{bot_id}:del")],
                         [btn("⬅️ Боты", "a:bots")]),
    )
    await cb.answer()
