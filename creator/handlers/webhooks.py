"""Owner- and admin-only screens with per-bot unsubscribe webhook URLs."""
from __future__ import annotations

import html

from aiogram.types import CallbackQuery

from creator.keyboards import btn, ikb
from creator.services import Ctx


def _lines(ctx: Ctx, bot_id: int) -> str | None:
    b = ctx.db.bot(bot_id)
    if not b or not getattr(ctx, "unsub", None):
        return None
    service = ctx.unsub
    uname = html.escape(b["username"] or "—")
    parts = [f"🔗 <b>Вебхуки отписок</b> для бота ID {bot_id} @{uname}\n",
             "Вставьте эти HTTPS-адреса в кабинеты сетей. Фабрика URL сама не ставит — "
             "параметра вебхука в API нет.\n"]
    tg = ctx.db.tgrass(bot_id)
    pf = ctx.db.pf(bot_id)
    if tg:
        url = html.escape(service.url(bot_id, "tgrass"))
        parts.append("TGrass — Настройки бота → Webhook → <b>отписка</b>:\n"
                     f"<code>{url}</code>\n")
    if pf:
        url = html.escape(service.url(bot_id, "piarflow"))
        parts.append("PiarFlow — кабинет → Webhook / API:\n"
                     f"<code>{url}</code>\n")
    if not tg and not pf:
        parts.append("Сети ещё не подключены к этому боту — адрес появится после одобрения заявки.")
    parts.append("\nНикому не пересылайте эти ссылки: кто их знает, сможет слать ложные отписки.")
    return "\n".join(parts)


async def show_owner(cb: CallbackQuery, ctx: Ctx, b) -> None:
    text = _lines(ctx, b["id"])
    if not text:
        await cb.answer("Недоступно", show_alert=True)
        return
    await cb.message.edit_text(text, reply_markup=ikb([btn("⬅️ К боту", f"b:{b['id']}:open")]),
                               disable_web_page_preview=True)
    await cb.answer()


async def show_admin(cb: CallbackQuery, ctx: Ctx, bot_id: int) -> None:
    text = _lines(ctx, bot_id)
    if not text:
        await cb.answer("Не найден", show_alert=True)
        return
    await cb.message.edit_text(text, reply_markup=ikb([btn("⬅️ К боту", f"a:bot:{bot_id}")]),
                               disable_web_page_preview=True)
    await cb.answer()
