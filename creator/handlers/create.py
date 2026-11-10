"""🔨 Создать бота: шаблон → токен → оплата → запуск."""
from __future__ import annotations

import html
import re
import sqlite3

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from child import templates
from creator.db import InsufficientFunds
from creator.keyboards import BTN_CREATE, btn, cancel_kb, ikb
from creator.money import rub
from creator.services import Ctx
from creator.states import CreateBot
from creator.template_selection import CONDITIONS_UNAVAILABLE, comparison_text, detail_kb, detail_text

router = Router(name="create")

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,50}$")

RENDER_ERRORS = (KeyError, ValueError, TypeError, OverflowError, sqlite3.Error)
STALE_SELECTION = "Действие устарело. Завершите текущую настройку или нажмите /cancel."
EDIT_UNAVAILABLE = "Не удалось обновить экран. Откройте «Создать бота» заново."


async def _selection_snapshot(cb: CallbackQuery, state: FSMContext) -> tuple[str | None, dict] | None:
    current = await state.get_state()
    if current not in (None, CreateBot.token.state):
        await cb.answer(STALE_SELECTION, show_alert=True)
        return None
    return current, await state.get_data()


async def _selection_unchanged(cb: CallbackQuery, state: FSMContext, snapshot: tuple[str | None, dict]) -> bool:
    # An edit is a network await: never overwrite a wizard opened while it was pending.
    if (await state.get_state(), await state.get_data()) != snapshot:
        await cb.answer(STALE_SELECTION, show_alert=True)
        return False
    return True


async def _edit_screen(cb: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup) -> bool:
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(text, reply_markup=reply_markup)
            return True
        except TelegramBadRequest as exc:
            if "message is not modified" in exc.message.lower():
                return True
        except TelegramAPIError:
            pass
    await cb.answer(EDIT_UNAVAILABLE, show_alert=True)
    return False


def templates_kb():
    return ikb(*[[btn(t.button, f"tpl:{t.key}")] for t in templates.all_infos()])


@router.message(F.text == BTN_CREATE)
async def choose(message: Message, ctx: Ctx) -> None:
    try:
        text = comparison_text(ctx)
    except RENDER_ERRORS:
        await message.answer(CONDITIONS_UNAVAILABLE)
        return
    await message.answer(text, reply_markup=templates_kb())


@router.callback_query(F.data == "create")
async def choose_cb(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    snapshot = await _selection_snapshot(cb, state)
    if snapshot is None:
        return
    try:
        text = comparison_text(ctx)
    except RENDER_ERRORS:
        await cb.answer(CONDITIONS_UNAVAILABLE, show_alert=True)
        return
    if not await _edit_screen(cb, text, templates_kb()):
        return
    if not await _selection_unchanged(cb, state, snapshot):
        return
    await state.clear()
    await cb.answer()


@router.callback_query(F.data.startswith("tpl:"))
async def template_selected(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    snapshot = await _selection_snapshot(cb, state)
    if snapshot is None:
        return
    key = cb.data.split(":", 1)[1]
    if key not in {"clicker", "refbot", "starbot"}:
        await cb.answer("Шаблон не найден. Откройте «Создать бота» заново.", show_alert=True)
        return
    try:
        text = detail_text(ctx, key)
    except RENDER_ERRORS:
        await cb.answer(CONDITIONS_UNAVAILABLE, show_alert=True)
        return
    if not await _edit_screen(cb, text, detail_kb(key)):
        return
    if not await _selection_unchanged(cb, state, snapshot):
        return
    await state.set_state(CreateBot.token)
    await state.update_data(template=key)
    await cb.answer()


@router.message(CreateBot.token, F.text)
async def token_received(message: Message, state: FSMContext, ctx: Ctx) -> None:
    token = message.text.strip()
    # Токен — секрет: удаляем сообщение из чата
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass
    if not TOKEN_RE.match(token):
        await message.answer("❌ Это не похоже на токен. Пришлите токен из @BotFather целиком.",
                             reply_markup=cancel_kb())
        return
    if ctx.db.token_in_use(token):
        await message.answer("❌ Этот бот уже подключён к конструктору.", reply_markup=cancel_kb())
        return
    checking = await message.answer("⏳ Проверяю токен…")
    me = await ctx.validate_token(token)
    if me is None:
        await checking.edit_text("❌ Токен недействителен. Проверьте его в @BotFather и пришлите ещё раз.",
                                 reply_markup=cancel_kb())
        return
    username, title = me
    data = await state.get_data()
    await state.clear()
    key = data.get("template", "clicker")
    t = templates.info(key)
    draft_id = ctx.db.create_draft(message.from_user.id, key, token, username, title)
    price = ctx.template_price(key)
    balance = ctx.db.user(message.from_user.id)["balance"]
    rows = []
    if ctx.lave is not None and ctx.lave.enabled and price > 0:
        rows.append([btn(f"💳 Оплатить {rub(price)} (LavePay)", f"lp:c:{draft_id}")])
    if balance >= price:
        label = "✅ Создать бесплатно" if price == 0 else f"💼 Оплатить с баланса ({rub(balance)})"
        rows.append([btn(label, f"bal:create:{draft_id}")])
    else:
        rows.append([btn("➕ Пополнить баланс", "cab:topup")])
    rows.append([btn("❌ Отмена", "cancel")])
    await checking.edit_text(
        f"✅ Токен принят!\n\n🤖 Бот: @{html.escape(username)} ({html.escape(title)})\n"
        f"📦 Шаблон: {t.button}\n💰 К оплате: <b>{rub(price)}</b>\n\n"
        "После оплаты бот будет автоматически запущен на нашем сервере.",
        reply_markup=ikb(*rows),
    )


@router.callback_query(F.data.startswith("bal:create:"))
async def balance_create(cb: CallbackQuery, ctx: Ctx) -> None:
    draft_id = int(cb.data.rsplit(":", 1)[1])
    try:
        bot_id = await ctx.create_bot_from_balance(cb.from_user.id, draft_id)
    except InsufficientFunds:
        await cb.answer("Недостаточно средств на балансе", show_alert=True)
        return
    if bot_id is None:
        await cb.answer("Этот заказ уже использован или устарел. Списание не выполнено. Создайте бота заново.", show_alert=True)
        return
    await cb.message.edit_text(f"✅ Бот ID {bot_id} создан и запускается! Откройте «🤖 Мои боты».")
    await cb.answer()
