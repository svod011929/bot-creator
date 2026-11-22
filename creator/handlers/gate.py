"""Обязательная подписка на канал новостей и чат конструктора."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, Message, TelegramObject

from creator.config import support_url
from creator.keyboards import btn, ikb, url_btn
from creator.services import Ctx

log = logging.getLogger("gate")
router = Router(name="gate")

# Documents stay readable without a subscription (info:docs and its children).
EXEMPT_CALLBACKS = {"gate:check"}
EXEMPT_PREFIXES = ("info:doc", "info:agreement", "info:privacy", "info:oferta")


def _username(url: str) -> str | None:
    """https://t.me/name -> @name; invite links return None."""
    u = support_url(url)
    if not u or "/+" in u or "joinchat" in u:
        return None
    path = u.removeprefix("https://t.me/").split("/")[0]
    return f"@{path}" if path else None


async def _is_member(bot: Bot, chat: str, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat, user_id)
        return m.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.RESTRICTED,
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("member check failed for %s: %s", chat, exc)
        return False


def _targets(ctx: Ctx) -> list[tuple[str, str]]:
    out = []
    ch = _username(ctx.cfg.channel_url)
    if ch:
        out.append(("📢 Канал новостей", ch))
    chat = _username(ctx.cfg.chat_url)
    if chat:
        out.append(("💬 Чат конструктора", chat))
    return out


class SubscribeMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        ctx: Ctx = data["ctx"]
        user = data.get("event_from_user")
        if user is None or ctx.is_admin(user.id):
            return await handler(event, data)

        targets = _targets(ctx)
        if not targets:
            return await handler(event, data)

        # Exempt the check itself and document viewing
        if isinstance(event, CallbackQuery):
            data_s = event.data or ""
            if data_s in EXEMPT_CALLBACKS or data_s.startswith(EXEMPT_PREFIXES):
                return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            # Still show the gate after welcome? We let start through, then gate on next action.
            return await handler(event, data)

        bot: Bot = data["bot"]
        missing = []
        for title, chat in targets:
            if not await _is_member(bot, chat, user.id):
                missing.append((title, chat))

        if not missing:
            return await handler(event, data)

        text = (
            "🔒 <b>Для использования конструктора нужна подписка</b>\n\n"
            "Подпишитесь на канал новостей и чат, затем нажмите «Проверить».\n\n"
            + "\n".join(f"• {t}" for t, _ in missing)
        )
        rows = []
        for title, chat in missing:
            rows.append([url_btn(title, f"https://t.me/{chat.lstrip('@')}")])
        rows.append([btn("✅ Проверить подписку", "gate:check")])
        markup = ikb(*rows)

        if isinstance(event, CallbackQuery):
            await event.answer()
            try:
                await event.message.edit_text(text, reply_markup=markup)
            except Exception:  # noqa: BLE001
                await event.message.answer(text, reply_markup=markup)
        else:
            await event.answer(text, reply_markup=markup)
        return None


@router.callback_query(F.data == "gate:check")
async def gate_check(cb: CallbackQuery, ctx: Ctx) -> None:
    targets = _targets(ctx)
    if not targets:
        await cb.answer("Подписка не требуется", show_alert=True)
        return
    missing = []
    for title, chat in targets:
        if not await _is_member(cb.bot, chat, cb.from_user.id):
            missing.append(title)
    if missing:
        await cb.answer("Ещё не подписаны: " + ", ".join(missing), show_alert=True)
        return
    await cb.answer("✅ Подписка подтверждена!")
    await cb.message.edit_text(
        "✅ Подписка подтверждена. Можете пользоваться конструктором.",
        reply_markup=ikb([btn("🏠 В меню", "info:back")]),
    )
