"""Read-only owner list/search/card navigation. Mutating card actions remain in mybots."""
from __future__ import annotations

import re
from dataclasses import replace

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from creator.bot_list import STATUSES, cached, data, fresh, normalize_query, remember, render
from creator.bot_setup import TOKEN_LIKE, bot_url
from creator.keyboards import bot_card_kb, btn, ikb
from creator.services import Ctx
from creator.states import BotList

router = Router(name="bot_list")
REQUEST = re.compile(r"bl:([0-9a-f]{8}):(search|back|reset|p:-?[0-9]{1,19}|f:(?:all|running|stopped|restarting|error))")
OPEN = re.compile(r"b:([1-9][0-9]{0,18}):open")
STALE = "Список или действие устарели. Откройте «Мои боты» заново."
ACTIVE = "Завершите текущую настройку или нажмите /cancel."
PROMPT = ("Пришлите ID бота или часть username (можно с @). Число — точный ID. "
          "Токен не нужен. До 32 символов username.")


def private(target) -> bool:
    message = target.message if isinstance(target, CallbackQuery) else target
    return (isinstance(message, Message) and message.chat.type == "private"
            and message.chat.id == target.from_user.id)


async def alert(target, text: str) -> None:
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)


async def send(target, text, markup) -> bool:
    try:
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        else:
            await target.answer(text, reply_markup=markup, disable_web_page_preview=True)
        return True
    except TelegramBadRequest as exc:
        if isinstance(target, CallbackQuery) and "message is not modified" in exc.message.lower():
            return True
    except TelegramAPIError:
        pass
    await alert(target, "Не удалось открыть экран. Откройте «Мои боты» заново.")
    return False


async def snapshot(state):
    return await state.get_state(), await state.get_data()


def own_search(before, view) -> bool:
    return before[0] == BotList.search.state and before[1].get("list_nonce") == view.nonce


async def display(target, state, ctx, view, before, notice="") -> bool:
    text, markup, view = render(ctx, target.from_user.id, view)
    if not await send(target, notice + text, markup):
        return False
    if await snapshot(state) != before:
        return False
    # A concurrent reset/search must not overwrite the newly selected context.
    current = cached(ctx, target.from_user.id)
    if current is not None and current.nonce != view.nonce:
        return False
    remember(ctx, target.from_user.id, view)
    if before[0] == BotList.search.state:
        await state.clear()
    return True


async def open_list(target, state: FSMContext, ctx: Ctx, fresh_view: bool = False):
    if not private(target):
        await alert(target, STALE)
        return
    before = await snapshot(state)
    view = cached(ctx, target.from_user.id)
    if before[0] is not None and (view is None or not own_search(before, view)):
        await alert(target, ACTIVE)
        return
    notice = ""
    if fresh_view or view is None:
        notice = "" if fresh_view else "Прежний список недоступен; открыт новый.\n"
        view = fresh()
        # Reserve the new nonce before network I/O: an in-flight old page must
        # not restore its context while this explicit fresh menu is being sent.
        remember(ctx, target.from_user.id, view)
    await display(target, state, ctx, view, before, notice)
    if isinstance(target, CallbackQuery):
        await target.answer()


@router.callback_query(F.data.startswith("bl:"))
async def callbacks(cb: CallbackQuery, state: FSMContext, ctx: Ctx):
    match = REQUEST.fullmatch(cb.data or "")
    view = cached(ctx, cb.from_user.id)
    if not private(cb) or not match or view is None or view.nonce != match[1]:
        await alert(cb, STALE)
        return
    action = match[2]
    before = await snapshot(state)
    if before[0] is not None and not (action in {"back", "reset"} and own_search(before, view)):
        await alert(cb, ACTIVE)
        return
    if action == "search":
        markup = ikb([btn("Назад к списку", data(view, "back"))],
                     [btn("Сбросить поиск и фильтр", data(view, "reset"))])
        if await send(cb, PROMPT, markup) and await snapshot(state) == before:
            current = cached(ctx, cb.from_user.id)
            if current is not None and current.nonce == view.nonce:
                await state.set_state(BotList.search)
                await state.set_data({"list_nonce": view.nonce})
    else:
        if action == "reset":
            view = replace(view, query="", status="all", page=0)
        elif action.startswith("p:"):
            view = replace(view, page=int(action[2:]))
        elif action.startswith("f:"):
            view = replace(view, status=action[2:], page=0)
        await display(cb, state, ctx, view, before)
    await cb.answer()


@router.message(BotList.search, F.text)
async def search_value(message: Message, state: FSMContext, ctx: Ctx):
    if not private(message):
        return
    before = await snapshot(state)
    view = cached(ctx, message.from_user.id)
    if view is None or not own_search(before, view):
        if await send(message, STALE, None) and await snapshot(state) == before:
            await state.clear()
        return
    try:
        query = normalize_query(message.text)
    except ValueError:
        await send(message, "Введите корректный ID или часть username. Токен не нужен.",
                   ikb([btn("Назад к списку", data(view, "back"))]))
        return
    await display(message, state, ctx, replace(view, query=query, page=0), before)


async def open_card(cb: CallbackQuery, state: FSMContext, ctx: Ctx):
    match = OPEN.fullmatch(cb.data or "")
    if not private(cb) or not match or int(match[1]) > 2 ** 63 - 1:
        await alert(cb, STALE)
        return
    row = ctx.db.bot(int(match[1]))
    if row is None or row["owner_id"] != cb.from_user.id:
        await alert(cb, "Бот не найден")
        return
    before = await snapshot(state)
    allowed = {"BotEdit:setting", "BotEdit:transfer", "BotEdit:sell_price"}
    if before[0] is not None and not (before[0] in allowed and before[1].get("bot_id") == row["id"]
                                    and before[1].get("template", row["template"]) == row["template"]):
        await alert(cb, ACTIVE)
        return
    from creator.handlers.mybots import card_text
    safe = dict(row)
    if not bot_url(row):
        safe["username"] = None
    if row["status"] not in STATUSES or row["status"] == "all":
        safe["status"] = "неизвестно"
    error = str(row["last_error"] or "")
    if row["token"]:
        error = error.replace(row["token"], "[скрыто]")
    safe["last_error"] = TOKEN_LIKE.sub("[скрыто]", error)
    try:
        text = card_text(ctx, safe)
        if row["token"]:
            text = text.replace(row["token"], "[скрыто]")
        text = TOKEN_LIKE.sub("[скрыто]", text)
    except (KeyError, ValueError, OSError):
        await alert(cb, "Сведения временно недоступны.")
        return
    identity = tuple(row[key] for key in ("owner_id", "template", "token"))

    def unchanged():
        current = ctx.db.bot(row["id"])
        return current is not None and tuple(current[key] for key in ("owner_id", "template", "token")) == identity

    if not unchanged():
        await alert(cb, "Бот не найден")
        return
    if await send(cb, text, bot_card_kb(row, row["sale_price"] is not None, ctx.extra_earn)):
        if unchanged() and await snapshot(state) == before and before[0] is not None:
            await state.clear()
    await cb.answer()
