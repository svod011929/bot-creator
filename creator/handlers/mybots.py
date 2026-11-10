"""🤖 Мои боты: список, карточка и управление ботом."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from child import templates
from child.files import dir_size, human_size, write_setting
from creator.db import InsufficientFunds
from creator.keyboards import BTN_MY, STATUS_TEXT, bot_card_kb, btn, cancel_kb, ikb
from creator.money import parse_rub, rub
from creator.services import Ctx
from creator.states import BotEdit
from creator.tgrass import child_users_for_card

router = Router(name="mybots")

HOSTING_OPTIONS = (7, 30, 90)


def _hosting_left_label(balance: int, day_price: int) -> str:
    if day_price <= 0:
        return "безлимит"
    days = balance // day_price
    if balance > 0 and days == 0:
        return "меньше суток"
    return f"≈{days} дн."


def _network_status_line(title: str, row) -> str:
    from creator.earnings_status import status_line
    return status_line(title, row)


def card_text(ctx: Ctx, b) -> str:
    t = templates.info(b["template"])
    day = ctx.price("HOSTING_DAY_PRICE")
    disk = human_size(dir_size(ctx.sup.bot_dir(b["id"])))
    users = child_users_for_card(ctx.sup.bot_dir(b["id"]))
    users_s = "н/д" if users is None else str(users)
    lines = [
        f"🤖 <b>Бот ID {b['id']}</b>\n",
        f"Статус: {STATUS_TEXT.get(b['status'], b['status'])}",
        f"Ссылка: @{html.escape(b['username'] or '—')}",
        f"Шаблон: {t.button}",
        f"Пользователи: {users_s}",
        f"Использование диска: {disk}",
        f"Баланс хостинга: {rub(b['hosting_balance'])} ({_hosting_left_label(b['hosting_balance'], day)})",
        f"VIP: {'👑 да' if b['vip'] else 'нет'}",
    ]
    if b["sale_price"] is not None:
        lines.append(f"🛒 Выставлен на продажу за {rub(b['sale_price'])}")
    if ctx.tgrass is not None:
        lines.append(_network_status_line("TGrass", ctx.db.tgrass(b["id"])))
    if ctx.piarflow is not None:
        lines.append(_network_status_line("PiarFlow", ctx.db.pf(b["id"])))
    if b["last_error"] and b["status"] != "running":
        # не светим потенциальные секреты из чужих систем — только уже сохранённый last_error процесса
        err = html.escape(str(b["last_error"])[:300])
        lines.append(f"\n⚠️ {err}")
    return "\n".join(lines)


async def show(cb_or_msg, text: str, kb) -> None:
    if isinstance(cb_or_msg, CallbackQuery):
        try:
            await cb_or_msg.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest:
            await cb_or_msg.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await cb_or_msg.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def show_card(target, ctx: Ctx, bot_id: int) -> None:
    b = ctx.db.bot(bot_id)
    if b:
        has_wh = bool(getattr(ctx, "unsub", None) and (ctx.db.tgrass(bot_id) or ctx.db.pf(bot_id)))
        await show(target, card_text(ctx, b), bot_card_kb(b, b["sale_price"] is not None, ctx.extra_earn, webhooks=has_wh))


@router.message(F.text == BTN_MY)
async def my_bots(message: Message, state: FSMContext, ctx: Ctx) -> None:
    from creator.handlers.bot_list import open_list
    await open_list(message, state, ctx, fresh_view=True)


@router.callback_query(F.data == "mybots")
async def my_bots_cb(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    from creator.handlers.bot_list import open_list
    await open_list(cb, state, ctx)


@router.callback_query(F.data.startswith("b:"))
async def bot_action(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    if len(parts) >= 3 and parts[2] in {"tg", "tgr", "pf"}:
        from creator.earnings_status import handle
        await handle(cb, state, ctx)
        return
    if len(parts) >= 3 and parts[2] in {"set", "sk"}:
        from creator.handlers.bot_setup import legacy_settings
        await legacy_settings(cb, state, ctx)
        return
    if len(parts) >= 3 and parts[2] == "open":
        from creator.handlers.bot_list import open_card
        await open_card(cb, state, ctx)
        return
    bot_id, action, args = int(parts[1]), parts[2], parts[3:]
    b = ctx.db.bot(bot_id)
    if not b or b["owner_id"] != cb.from_user.id:
        await cb.answer("Бот не найден", show_alert=True)
        return
    day = ctx.price("HOSTING_DAY_PRICE")
    await state.clear()

    if action == "wh":
        from creator.handlers.webhooks import show_owner
        await show_owner(cb, ctx, b)
        return

    if action == "start":
        if day and b["hosting_balance"] < day:
            await cb.answer("Недостаточно средств на хостинге. Пополните «💳 Хостинг».", show_alert=True)
            return
        await ctx.sup.start(bot_id)
        await cb.answer("▶️ Бот запускается")
        await show_card(cb, ctx, bot_id)
        return

    elif action == "stop":
        await ctx.sup.stop(bot_id)
        await cb.answer("⏹ Бот остановлен")
        await show_card(cb, ctx, bot_id)
        return

    elif action == "restart":
        if not b["desired"]:
            await cb.answer("Бот остановлен — нажмите «Запустить»", show_alert=True)
            return
        await ctx.sup.restart(bot_id)
        await cb.answer("🔄 Бот перезапущен")
        await show_card(cb, ctx, bot_id)
        return

    elif action == "transfer":
        await state.set_state(BotEdit.transfer)
        await state.update_data(bot_id=bot_id)
        await show(cb, "🔁 Пришлите Telegram ID пользователя, которому хотите передать бота.\n"
                       "Он должен хотя бы раз запустить конструктор.", cancel_kb(f"b:{bot_id}:open"))

    elif action == "transfer_ok":
        b2 = ctx.db.bot(bot_id)
        if not b2 or b2["deleted"] or b2["owner_id"] != cb.from_user.id:
            await cb.answer("Бот не найден", show_alert=True)
            return
        new_owner = int(args[0])
        if new_owner == cb.from_user.id:
            await cb.answer("Нельзя передать бота самому себе", show_alert=True)
            return
        if not ctx.db.user(new_owner):
            await cb.answer("Пользователь не найден", show_alert=True)
            return
        ctx.db.transfer_bot(bot_id, new_owner)
        await ctx.apply_changes(bot_id)
        await show(cb, f"✅ Бот ID {bot_id} передан пользователю <code>{new_owner}</code>.", ikb([btn("⬅️ Назад", "mybots")]))
        try:
            await cb.bot.send_message(new_owner, f"🎁 Вам передали бота ID {bot_id} (@{b2['username']}). "
                                                 "Он в разделе «🤖 Мои боты».")
        except Exception:  # noqa: BLE001
            pass

    elif action == "host":
        rows = []
        if day > 0:
            lave = ctx.lave is not None and ctx.lave.enabled
            for d in HOSTING_OPTIONS:
                price = ctx.hosting_pack_price(d)
                row = [btn(f"💳 +{d} дн. — {rub(price)}", f"lp:h:{bot_id}:{d}")] if lave else []
                row.append(btn(f"💼 +{d} дн. с баланса" if lave else f"💼 +{d} дн. — {rub(price)} с баланса",
                               f"b:{bot_id}:hbal:{d}"))
                rows.append(row)
        rows.append([btn("⬅️ Назад", f"b:{bot_id}:open")])
        await show(cb, f"💳 <b>Хостинг бота ID {bot_id}</b>\n\nБаланс: {rub(b['hosting_balance'])}\n"
                       f"Списание: {rub(day)} в сутки, пока бот запущен.\n"
                       "Когда баланс закончится, бот будет остановлен.", ikb(*rows))

    elif action == "hbal":
        days = int(args[0])
        if days not in HOSTING_OPTIONS:
            await cb.answer()
            return
        amount = ctx.hosting_pack_price(days)
        if amount <= 0:
            await cb.answer("Хостинг сейчас бесплатный 🙂", show_alert=True)
            return
        try:
            ctx.db.hosting_from_balance(cb.from_user.id, bot_id, amount)
        except InsufficientFunds:
            await cb.answer("Недостаточно средств на балансе. Пополните в «💼 Мой кабинет».", show_alert=True)
            return
        await cb.answer(f"✅ Хостинг пополнен на {days} дн. ({rub(amount)})", show_alert=True)
        await show_card(cb, ctx, bot_id)
        return

    elif action == "vip":
        if b["vip"]:
            await cb.answer("👑 VIP уже активен", show_alert=True)
            return
        price = ctx.price("VIP_PRICE")
        await show(cb, f"👑 <b>VIP для бота ID {bot_id}</b>\n\n• Убирает подпись «Бот создан в @{ctx.bot_username}»\n"
                       f"• Приоритетная поддержка\n\nЦена: {rub(price)} навсегда",
                   ikb([btn(f"💳 Оплатить {rub(price)} (LavePay)", f"lp:v:{bot_id}")]
                       if ctx.lave is not None and ctx.lave.enabled and price > 0 else [],
                       [btn("💼 С баланса", f"b:{bot_id}:vbal")],
                       [btn("⬅️ Назад", f"b:{bot_id}:open")]))

    elif action == "vbal":
        if b["vip"]:
            await cb.answer("👑 VIP уже активен", show_alert=True)
            return
        try:
            ctx.db.change_balance(cb.from_user.id, -ctx.price("VIP_PRICE"), "vip", f"VIP бота ID {bot_id}")
        except InsufficientFunds:
            await cb.answer("Недостаточно средств на балансе", show_alert=True)
            return
        await ctx.set_vip(bot_id)
        await cb.answer("👑 VIP активирован!", show_alert=True)
        await show_card(cb, ctx, bot_id)
        return

    elif action == "sell":
        await state.set_state(BotEdit.sell_price)
        await state.update_data(bot_id=bot_id)
        fee = ctx.price("RESALE_FEE_PERCENT")
        await show(cb, f"🛒 Введите цену продажи в ₽, например 500 или 499,90 (комиссия сервиса {fee}%).\n"
                       "Покупатель оплатит с баланса, деньги придут на ваш баланс.", cancel_kb(f"b:{bot_id}:open"))

    elif action == "unsell":
        ctx.db.update_bot(bot_id, sale_price=None)
        await cb.answer("Снят с продажи")
        await show_card(cb, ctx, bot_id)
        return

    elif action == "tg" and ctx.extra_earn:
        from creator.handlers import piarflow as pf
        from creator.handlers import tgrass as tg
        if ctx.tgrass is not None and ctx.piarflow is not None:
            await show(cb, *pf.hub_screen(ctx, b))
        elif ctx.tgrass is not None:
            await show(cb, *tg.owner_screen(ctx, b))
        else:
            await show(cb, *pf.owner_screen(ctx, b))

    elif action in ("tgr", "tgapply", "tgmod") and ctx.tgrass is not None:
        from creator.handlers import tgrass as tg
        if action == "tgr":
            await show(cb, *tg.owner_screen(ctx, b))
        elif action == "tgmod":
            await tg.owner_apply(cb, ctx, b, mode=(args[0] if args else None))
            return
        else:
            await tg.owner_apply(cb, ctx, b)
            return

    elif action in ("pf", "pfapply", "pfmod") and ctx.piarflow is not None:
        from creator.handlers import piarflow as pf
        if action == "pf":
            await show(cb, *pf.owner_screen(ctx, b))
        elif action == "pfmod":
            await pf.owner_apply(cb, ctx, b, mode=(args[0] if args else None))
            return
        else:
            await pf.owner_apply(cb, ctx, b)
            return

    elif action == "del":
        await show(cb, f"🗑 Удалить бота ID {bot_id} (@{html.escape(b['username'] or '')})?\n\n"
                       "Бот будет остановлен, данные пользователей удалены. Баланс хостинга не возвращается.",
                   ikb([btn("✅ Да, удалить", f"b:{bot_id}:delok"), btn("❌ Нет", f"b:{bot_id}:open")]))

    elif action == "delok":
        b2 = ctx.db.bot(bot_id)
        if not b2 or b2["deleted"] or b2["owner_id"] != cb.from_user.id:
            await cb.answer("Бот не найден", show_alert=True)
            return
        await ctx.delete_bot(bot_id)
        await show(cb, f"🗑 Бот ID {bot_id} удалён.", ikb([btn("⬅️ Мои боты", "mybots")]))

    await cb.answer()


@router.message(BotEdit.transfer, F.text)
async def transfer_id(message: Message, state: FSMContext, ctx: Ctx) -> None:
    data = await state.get_data()
    raw = message.text.strip()
    if not raw.isdigit():
        await message.answer("Пришлите числовой Telegram ID.")
        return
    uid = int(raw)
    if uid == message.from_user.id or not ctx.db.user(uid):
        await message.answer("❌ Пользователь не найден в конструкторе (или это вы).")
        return
    await state.clear()
    bot_id = data["bot_id"]
    await message.answer(f"Передать бота ID {bot_id} пользователю <code>{uid}</code>? Отменить это будет нельзя.",
                         reply_markup=ikb([btn("✅ Передать", f"b:{bot_id}:transfer_ok:{uid}"),
                                           btn("❌ Отмена", f"b:{bot_id}:open")]))


@router.message(BotEdit.setting, F.text)
async def setting_value(message: Message, state: FSMContext, ctx: Ctx) -> None:
    data = await state.get_data()
    bot_id = data.get("bot_id")
    b = ctx.db.bot(bot_id) if type(bot_id) is int and 0 < bot_id <= 2 ** 63 - 1 else None
    if (not b or b["owner_id"] != message.from_user.id
            or data.get("template", b["template"]) != b["template"] or not isinstance(data.get("key"), str)):
        await state.clear()
        await message.answer("Настройка устарела. Откройте настройки бота заново.")
        return
    if data.get("key") in {"required_channel", "channel_url"}:
        from creator.handlers.subscription import screen
        await screen(message, state, ctx, b["id"])
        return
    try:
        st = templates.info(b["template"]).setting(data.get("key"))
    except KeyError:
        st = None
    if st is None:
        await state.clear()
        await message.answer("Настройка устарела. Откройте настройки бота заново.")
        return
    try:
        value = st.parse(message.text)
    except ValueError:
        await message.answer("❌ Неверное значение, попробуйте ещё раз.")
        return
    await state.clear()
    write_setting(ctx.sup.bot_dir(b["id"]), st.key, value)
    await message.answer(f"✅ «{st.label}» сохранено.", reply_markup=ikb([btn("⚙️ К настройкам", f"b:{b['id']}:set")]))


@router.message(BotEdit.sell_price, F.text)
async def sell_price(message: Message, state: FSMContext, ctx: Ctx) -> None:
    data = await state.get_data()
    price = parse_rub(message.text)
    if price is None or not 100 <= price <= 100_000_000:
        await message.answer("Введите цену в рублях от 1 до 1 000 000, например 500 или 499,90.")
        return
    await state.clear()
    b = ctx.db.bot(data["bot_id"])
    if not b or b["owner_id"] != message.from_user.id:
        return
    ctx.db.update_bot(b["id"], sale_price=price)
    await message.answer(f"✅ Бот ID {b['id']} выставлен на продажу за {rub(price)} в разделе «🛒 Перепродажа».",
                         reply_markup=ikb([btn("⬅️ К боту", f"b:{b['id']}:open")]))
