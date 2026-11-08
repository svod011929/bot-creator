"""Private owner navigation; existing editor/wizard, no live checks or money operations."""
from __future__ import annotations

import html
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from child import templates
from creator.bot_setup import CHANNEL_KEYS, TOKEN_LIKE, load_snapshot, view
from creator.keyboards import bot_card_kb, cancel_kb
from creator.services import Ctx
from creator.states import BotEdit

router = Router(name="bot_setup")
REQUEST = re.compile(
    r"setup:([1-9][0-9]{0,18}):(clicker|refbot|starbot):"
    r"(ready|settings|card|channel|group:(appearance|rules)|edit:[a-z_]+)"
)
LEGACY = re.compile(r"b:([1-9][0-9]{0,18}):(set|sk:([a-z_]+))")
UNAVAILABLE = "Бот не найден или действие устарело."
ACTIVE = "Завершите текущую настройку или нажмите /cancel."


def _identity(row) -> tuple:
    return row["owner_id"], row["template"], row["token"]


def _still_owned(ctx: Ctx, row) -> bool:
    latest = ctx.db.bot(row["id"])
    return latest is not None and _identity(latest) == _identity(row)


def _may_leave(current, data, row, uid, action) -> bool:
    if current is None:
        return True
    if action not in {"settings", "card"} or data.get("bot_id") != row["id"]:
        return False
    if current == BotEdit.setting.state:
        return data.get("template", row["template"]) == row["template"]
    return current == "Wizard:active" and data.get("stage") == "status" and data.get("user_id") == uid


async def _deliver(cb: CallbackQuery, text, markup) -> bool:
    try:
        await cb.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in exc.message.lower():
            return True
    except TelegramAPIError:
        pass
    await cb.answer("Не удалось открыть экран. Откройте «Мои боты» заново.", show_alert=True)
    return False


async def legacy_settings(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await handle(cb, state, ctx, legacy=True)


@router.callback_query(F.data.startswith("setup:"))
async def handle(cb: CallbackQuery, state: FSMContext, ctx: Ctx, legacy: bool = False) -> None:
    match = (LEGACY if legacy else REQUEST).fullmatch(cb.data or "")
    if not match or int(match[1]) > 2 ** 63 - 1:
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    row = ctx.db.bot(int(match[1]))
    if (not isinstance(cb.message, Message) or cb.message.chat.type != "private"
            or cb.message.chat.id != cb.from_user.id or not row or row["owner_id"] != cb.from_user.id
            or row["template"] not in templates.TEMPLATE_MODULES or not legacy and row["template"] != match[2]):
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    if legacy:
        action, arg = ("settings", None) if match[2] == "set" else ("edit", match[3])
        if arg in CHANNEL_KEYS:
            action, arg = "channel", None
    else:
        action, *args = match[3].split(":")
        arg = args[0] if args else None
    setting = templates.info(row["template"]).setting(arg) if action == "edit" else None
    if action == "edit" and (setting is None or arg in CHANNEL_KEYS):
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    snapshot = await state.get_state(), await state.get_data()
    if not _may_leave(*snapshot, row, cb.from_user.id, action):
        await cb.answer(ACTIVE, show_alert=True)
        return
    if not _still_owned(ctx, row):
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    if action == "channel":
        if not load_snapshot(ctx, row).editable:
            await cb.answer("Сведения временно недоступны.", show_alert=True)
            return
        from creator.handlers.subscription import screen
        await screen(cb, state, ctx, row["id"])
        await cb.answer()
        return
    try:
        if action == "card":
            from creator.handlers.mybots import card_text
            # Existing card truncates errors; redact first so even a token fragment
            # cannot escape at that truncation boundary through the new back route.
            safe_row = dict(row)
            error = str(row["last_error"] or "")
            if row["token"]:
                error = error.replace(row["token"], "[скрыто]")
            safe_row["last_error"] = TOKEN_LIKE.sub("[скрыто]", error)
            text = card_text(ctx, safe_row)
            if row["token"]:
                text = text.replace(row["token"], "[скрыто]")
            text = TOKEN_LIKE.sub("[скрыто]", text)
            markup = bot_card_kb(row, row["sale_price"] is not None, ctx.extra_earn)
        elif action == "edit":
            if not load_snapshot(ctx, row).editable:
                await cb.answer("Сведения временно недоступны.", show_alert=True)
                return
            text = f"✏️ Введите новое значение для «{html.escape(setting.label)}»"
            markup = cancel_kb(f"b:{row['id']}:set")
        else:
            text, markup = view(ctx, row, action, arg)
    except (ValueError, KeyError, TypeError, OSError):
        await cb.answer("Сведения временно недоступны.", show_alert=True)
        return
    # File reads/rendering may observe changing ownership or template metadata.
    if not _still_owned(ctx, row):
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    if not await _deliver(cb, text, markup):
        return
    if not _still_owned(ctx, row) or (await state.get_state(), await state.get_data()) != snapshot:
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    if action == "edit":
        await state.set_state(BotEdit.setting)
        await state.set_data({"bot_id": row["id"], "key": setting.key, "template": row["template"]})
    elif snapshot[0] is not None:
        await state.clear()
    await cb.answer()
