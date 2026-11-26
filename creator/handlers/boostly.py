"""🚀 Boostly: заявка владельца и одобрение в админке. Хаб-ключ в чат не выводится."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from creator.boostly import BoostlyError
from creator.keyboards import btn, ikb
from creator.money import rub
from creator.services import Ctx
from creator.tgrass import child_users_count

log = logging.getLogger("boostly.ui")

router = Router(name="boostly")

STATUS = {
    "pending": "⏳ заявка на рассмотрении",
    "approved": "✅ подключён (трансляция через конструктор)",
    "rejected": "❌ заявка отклонена",
    "disabled": "⛔ отключён администратором",
}


def _bot_of(ctx: Ctx, bot_id: int, uid: int):
    b = ctx.db.bot(bot_id)
    if not b or b["owner_id"] != uid or b["deleted"]:
        return None
    return b


@router.callback_query(F.data.regexp(r"^b:\d+:bl$"))
async def screen(cb: CallbackQuery, ctx: Ctx) -> None:
    bot_id = int(cb.data.split(":")[1])
    b = _bot_of(ctx, bot_id, cb.from_user.id)
    if not b:
        await cb.answer("Бот не найден", show_alert=True)
        return
    users = child_users_count(ctx.sup.bot_dir(bot_id))
    row = ctx.db.boostly(bot_id)
    reward = rub(ctx.price("BOOSTLY_REWARD"))
    need = int(ctx.price("BOOSTLY_MIN_USERS"))
    if row and row["status"] == "approved":
        text = (
            f"🚀 <b>Boostly · бот ID {bot_id}</b>\n\n"
            f"Статус: {STATUS['approved']}\n"
            f"Пользователей: {users}\n"
            f"Награда владельцу: {reward} за подтверждённое задание.\n\n"
            "Задания приходят от площадки через конструктор. Ключ площадки в бота не передаётся."
        )
        kb = ikb([btn("⬅️ Назад", f"b:{bot_id}:open")])
    elif row and row["status"] == "pending":
        text = (
            f"🚀 <b>Boostly · бот ID {bot_id}</b>\n\n"
            f"Статус: {STATUS['pending']}\n"
            f"Пользователей: {users}"
        )
        kb = ikb([btn("⬅️ Назад", f"b:{bot_id}:open")])
    elif row and row["status"] == "disabled":
        text = (
            f"🚀 <b>Boostly · бот ID {bot_id}</b>\n\n"
            f"Статус: {STATUS['disabled']}\n"
            f"Пользователей: {users}"
        )
        kb = ikb(
            [btn("📝 Подать заявку снова", f"b:{bot_id}:blapply")],
            [btn("⬅️ Назад", f"b:{bot_id}:open")],
        )
    else:
        extra = f"Минимум пользователей: {need}. " if need > 0 else ""
        text = (
            f"🚀 <b>Boostly · бот ID {bot_id}</b>\n\n"
            f"Пользователей: {users}\n\n"
            f"{extra}Подключить бота к Boostly? За каждую подтверждённую "
            f"задачу вы получаете {reward} на баланс конструктора."
        )
        kb = ikb(
            [btn("📝 Подать заявку", f"b:{bot_id}:blapply")],
            [btn("⬅️ Назад", f"b:{bot_id}:open")],
        )
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^b:\d+:blapply$"))
async def apply(cb: CallbackQuery, ctx: Ctx) -> None:
    bot_id = int(cb.data.split(":")[1])
    b = _bot_of(ctx, bot_id, cb.from_user.id)
    if not b:
        await cb.answer("Бот не найден", show_alert=True)
        return
    users = child_users_count(ctx.sup.bot_dir(bot_id))
    need = int(ctx.price("BOOSTLY_MIN_USERS"))
    if users < need:
        await cb.answer(f"Нужно минимум {need} пользователей в боте, сейчас {users}.", show_alert=True)
        return
    if not ctx.db.boostly_apply(bot_id, cb.from_user.id, users):
        await cb.answer("Заявка уже подана или бот подключён", show_alert=True)
        return
    await cb.answer("Заявка отправлена")
    try:
        await cb.message.edit_text(
            f"📝 Заявка на Boostly для бота ID {bot_id} отправлена.\n"
            "Администратор рассмотрит её.",
            reply_markup=ikb([btn("⬅️ Назад", f"b:{bot_id}:open")]),
        )
    except TelegramBadRequest:
        pass
    for admin in ctx.cfg.admin_ids:
        try:
            await cb.bot.send_message(
                admin,
                f"🚀 Заявка Boostly: бот ID {bot_id} @{html.escape(b['username'] or '—')}\n"
                f"Владелец: <code>{cb.from_user.id}</code> · пользователей: {users}",
                reply_markup=ikb(
                    [btn("✅ Одобрить", f"a:bl:ok:{bot_id}"), btn("❌ Отклонить", f"a:bl:no:{bot_id}")],
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.info("notify admin %s failed: %s", admin, e)


@router.callback_query(F.data.startswith("a:bl:ok:"))
async def admin_ok(cb: CallbackQuery, ctx: Ctx) -> None:
    if not ctx.is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    bot_id = int(cb.data.split(":")[-1])
    svc = ctx.boostly
    if svc is None:
        await cb.answer("Boostly не доступен", show_alert=True)
        return
    try:
        await svc.approve(bot_id, cb.from_user.id)
    except BoostlyError as e:
        await cb.answer(e.message, show_alert=True)
        return
    except LookupError:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    await cb.answer("Подключено")
    b = ctx.db.bot(bot_id)
    try:
        await cb.message.edit_text(
            f"✅ Бот ID {bot_id} подключён к Boostly. Ключ площадки остаётся в конструкторе.",
            reply_markup=ikb([btn("🚀 Заявки Boostly", "a:bl:list")]),
        )
    except TelegramBadRequest:
        pass
    if b:
        try:
            await cb.bot.send_message(
                b["owner_id"],
                f"✅ Бот ID {bot_id} подключён к Boostly. "
                f"За каждое подтверждённое задание вы получаете {rub(ctx.price('BOOSTLY_REWARD'))} "
                "на баланс конструктора.",
            )
        except Exception:  # noqa: BLE001
            pass


@router.callback_query(F.data.startswith("a:bl:no:"))
async def admin_no(cb: CallbackQuery, ctx: Ctx) -> None:
    if not ctx.is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    bot_id = int(cb.data.split(":")[-1])
    svc = ctx.boostly
    if svc is None or not await svc.reject(bot_id, cb.from_user.id):
        await cb.answer("Заявку нельзя отклонить", show_alert=True)
        return
    await cb.answer("Отклонено")
    try:
        await cb.message.edit_text(
            f"❌ Заявка бота ID {bot_id} отклонена.",
            reply_markup=ikb([btn("🚀 Заявки Boostly", "a:bl:list")]),
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "a:bl:list")
async def admin_list(cb: CallbackQuery, ctx: Ctx) -> None:
    if not ctx.is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    rows = ctx.db.all(
        "SELECT t.*, b.username FROM boostly_bots t JOIN bots b ON b.id=t.bot_id "
        "WHERE t.status='pending' AND b.deleted=0 ORDER BY t.applied_at"
    )
    if not rows:
        await cb.message.edit_text("Нет заявок на Boostly.", reply_markup=ikb([btn("⬅️ Назад", "a:main")]))
        await cb.answer()
        return
    lines = ["🚀 <b>Заявки Boostly</b>\n"]
    kb = []
    for r in rows:
        lines.append(f"• ID {r['bot_id']} @{html.escape(r['username'] or '—')} · польз. {r['users_at_apply']}")
        kb.append([
            btn(f"✅ Одобрить {r['bot_id']}", f"a:bl:ok:{r['bot_id']}"),
            btn(f"❌ Отклонить {r['bot_id']}", f"a:bl:no:{r['bot_id']}"),
        ])
    kb.append([btn("⬅️ Назад", "a:main")])
    await cb.message.edit_text("\n".join(lines), reply_markup=ikb(*kb))
    await cb.answer()
