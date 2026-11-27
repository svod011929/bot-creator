"""Owner- and admin-only screens with per-bot unsubscribe webhook URLs."""
from __future__ import annotations

import html

from aiogram.types import CallbackQuery

from creator.keyboards import btn, ikb
from creator.services import Ctx
from creator.ui import copy_btn, screen
from creator.ui import show as ui_show
from creator.unsubscribe_webhooks import PF_PASTE, TG_PASTE


def _lines(ctx: Ctx, bot_id: int) -> tuple[str, object] | None:
    b = ctx.db.bot(bot_id)
    if not b or not getattr(ctx, "unsub", None):
        return None
    service = ctx.unsub
    uname = html.escape(b["username"] or "—")
    main = [
        f"Для бота #{bot_id} @{uname}",
        "Вставьте эти HTTPS-адреса в кабинеты сетей. Фабрика URL сама не ставит.",
    ]
    det = []
    kb_rows = []
    tg = ctx.db.tgrass(bot_id)
    pf = ctx.db.pf(bot_id)
    if tg:
        url = service.url(bot_id, "tgrass")
        det.append(f"{TG_PASTE}\n<code>{html.escape(url)}</code>")
        # TGrass URL includes the webhook secret; never attach copy_btn to it.
    if pf:
        url = service.url(bot_id, "piarflow")
        det.append(f"{PF_PASTE}\n<code>{html.escape(url)}</code>")
        det.append("Один адрес на весь аккаунт PiarFlow, без секрета. Событие уходит нужному боту по bot_id.")
        kb_rows.append([copy_btn("📋 Копировать PiarFlow", url)])
    if not tg and not pf:
        det.append("Сети ещё не подключены к этому боту — адрес появится после одобрения заявки.")
    det.append("Никому не пересылайте эти ссылки: кто их знает, сможет слать ложные отписки.")
    text = screen("Вебхуки отписок", *main, details_lines=det, expandable=True)
    return text, ikb(*kb_rows, [btn("⬅️ К боту", f"b:{bot_id}:open")])


async def show_owner(cb: CallbackQuery, ctx: Ctx, b) -> None:
    result = _lines(ctx, b["id"])
    if not result:
        await cb.answer("Недоступно", show_alert=True)
        return
    text, kb = result
    await ui_show(cb, text, kb)
    await cb.answer()


async def show_admin(cb: CallbackQuery, ctx: Ctx, bot_id: int) -> None:
    result = _lines(ctx, bot_id)
    if not result:
        await cb.answer("Не найден", show_alert=True)
        return
    text, kb = result
    # Override back button for admin
    kb = ikb(*kb.inline_keyboard[:-1], [btn("⬅️ К боту", f"a:bot:{bot_id}")])
    await ui_show(cb, text, kb)
    await cb.answer()


async def show_hub(cb: CallbackQuery, ctx: Ctx) -> None:
    if not getattr(ctx, "unsub", None):
        await cb.answer("Недоступно", show_alert=True)
        return
    service = ctx.unsub
    main = [
        "Эти адреса — для режима трансляции (общий ключ источника креатора).",
        "Вставьте их в кабинете хаб-бота сетей. Владельцам дочерних ботов их не отправляйте.",
    ]
    det = [
        f"{TG_PASTE}\n<code>{html.escape(service.hub_url('tgrass'))}</code>",
        f"{PF_PASTE} (один на весь аккаунт, без секрета)\n<code>{html.escape(service.shared_piarflow_url())}</code>",
        "Штраф за отписку равен начисленной награде и действует после того, как адрес установлен.",
        "Никому не пересылайте эти ссылки: кто их знает, сможет слать ложные отписки.",
    ]
    text = screen("Вебхуки трансляции спонсоров", *main, details_lines=det, expandable=True)
    await ui_show(cb, text, ikb([btn("⬅️ В админ-панель", "a:main")]))
    await cb.answer()
