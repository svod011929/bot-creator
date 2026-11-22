"""Обязательная подписка на канал новостей и чат конструктора.

Принимает публичные @username и частные invite-ссылки (t.me/+…, joinchat).
Для частных ссылок проверка идёт по числовому chat_id: CHANNEL_CHAT_ID / CHAT_CHAT_ID
или после того, как админ переслал боту пост из канала или чата. Бот должен быть
администратором в частном канале или чате.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject

from creator.config import support_url
from creator.keyboards import btn, ikb, url_btn
from creator.services import Ctx

log = logging.getLogger("gate")
router = Router(name="gate")

INVITE_RE = re.compile(r"https://t\.me/(?:\+|joinchat/)[A-Za-z0-9_-]+")
MEMBER_OK = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.RESTRICTED,
}

# Documents stay readable without a subscription (info:docs and its children).
EXEMPT_CALLBACKS = {"gate:check"}
EXEMPT_PREFIXES = ("info:doc", "info:agreement", "info:privacy", "info:oferta")


def _is_invite(url: str) -> bool:
    u = support_url(url) or (url or "").strip()
    return bool(INVITE_RE.fullmatch(u))


def _public_username(url: str) -> str | None:
    u = support_url(url)
    if not u or _is_invite(u):
        return None
    path = u.removeprefix("https://t.me/").split("/")[0]
    return f"@{path}" if path else None


def _username(url: str) -> str | None:
    """https://t.me/name -> @name; invite links return None."""
    return _public_username(url)


def _link(url: str) -> str:
    return support_url(url) or (url or "").strip()


def _bound_id(ctx: Ctx, kind: str) -> int:
    cfg_val = ctx.cfg.channel_chat_id if kind == "channel" else ctx.cfg.chat_chat_id
    kv_key = "gate_channel_id" if kind == "channel" else "gate_chat_id"
    try:
        return int(cfg_val or ctx.db.kv_get(kv_key) or 0)
    except (TypeError, ValueError):
        return 0


def channel_dest(ctx: Ctx) -> str | int | None:
    """Публичный @username канала или числовой id, если частная ссылка уже привязана."""
    name = _public_username(ctx.cfg.channel_url)
    if name:
        return name
    cid = _bound_id(ctx, "channel")
    return cid or None


async def _is_member(bot: Bot, chat: str | int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat, user_id)
        return m.status in MEMBER_OK
    except Exception as exc:  # noqa: BLE001
        log.debug("member check failed for %s: %s", chat, exc)
        return False


def _targets(ctx: Ctx) -> list[dict]:
    """Цели: title, link (для кнопки), check (username или chat_id)."""
    out: list[dict] = []
    for title, url, kind in (
        ("📢 Канал новостей", ctx.cfg.channel_url, "channel"),
        ("💬 Чат конструктора", ctx.cfg.chat_url, "chat"),
    ):
        link = _link(url)
        if not link:
            continue
        check = _public_username(link) or _bound_id(ctx, kind) or None
        if check:
            out.append({"title": title, "link": link, "check": check})
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

        if isinstance(event, CallbackQuery):
            data_s = event.data or ""
            if data_s in EXEMPT_CALLBACKS or data_s.startswith(EXEMPT_PREFIXES):
                return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)

        bot: Bot = data["bot"]
        missing = [t for t in targets if not await _is_member(bot, t["check"], user.id)]
        if not missing:
            return await handler(event, data)

        text = (
            "🔒 <b>Для использования конструктора нужна подписка</b>\n\n"
            "Подпишитесь и нажмите «Проверить».\n\n"
            + "\n".join(f"• {t['title']}" for t in missing)
        )
        rows = [[url_btn(t["title"], t["link"])] for t in missing]
        rows.append([btn("✅ Проверить подписку", "gate:check")])
        markup = ikb(*rows)

        if isinstance(event, CallbackQuery):
            await event.answer()
            try:
                await event.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                await event.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
        else:
            await event.answer(text, reply_markup=markup, disable_web_page_preview=True)
        return None


@router.callback_query(F.data == "gate:check")
async def gate_check(cb: CallbackQuery, ctx: Ctx) -> None:
    targets = _targets(ctx)
    if not targets:
        await cb.answer("Подписка не требуется", show_alert=True)
        return
    missing = [t["title"] for t in targets if not await _is_member(cb.bot, t["check"], cb.from_user.id)]
    if missing:
        await cb.answer("Ещё не подписаны: " + ", ".join(missing), show_alert=True)
        return
    await cb.answer("✅ Подписка подтверждена!")
    await cb.message.edit_text(
        "✅ Подписка подтверждена. Можете пользоваться конструктором.",
        reply_markup=ikb([btn("🏠 В меню", "info:back")]),
        disable_web_page_preview=True,
    )


class AdminForward(Filter):
    """Только пересланные админом посты, чтобы обычные пересылки не перехватывались."""

    async def __call__(self, message: Message, ctx: Ctx) -> bool:
        user = message.from_user
        return bool(message.forward_origin and user and ctx.is_admin(user.id))


@router.message(AdminForward())
async def bind_private(message: Message, ctx: Ctx) -> None:
    """Админ пересылает пост из канала/чата — бот запоминает chat_id для частной ссылки."""
    origin = message.forward_origin
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if not chat:
        return
    key = "gate_channel_id" if chat.type == "channel" else "gate_chat_id"
    ctx.db.kv_set(key, str(chat.id))
    await message.answer(
        f"✅ Привязано: {chat.type} <code>{chat.id}</code> ({html.escape(chat.title or '')}).\n"
        "Теперь частная ссылка в CHANNEL_URL / CHAT_URL будет проверяться по этому ID.",
        disable_web_page_preview=True,
    )
