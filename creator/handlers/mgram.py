"""Заработок на рассылках: подключение бота к системе монетизации (без упоминания провайдера)."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery

from creator.keyboards import btn, ikb
from creator.mgram_factory import MgramError, MgramFactory
from creator.services import Ctx
from creator.tgrass import child_users_count

router = Router(name="mgram")

MIN_USERS = 50


def _factory(ctx: Ctx) -> MgramFactory | None:
    key = getattr(ctx.cfg, "mgram_partner_api_key", "")
    return MgramFactory(key) if key else None


def _users(ctx: Ctx, bot_id: int) -> int:
    return child_users_count(ctx.sup.bot_dir(bot_id))


@router.callback_query(F.data.startswith("b:") & F.data.endswith(":mgram"))
async def mgram_screen(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer("Неверная кнопка", show_alert=True)
        return
    bot_id = int(parts[1])
    b = ctx.db.bot(bot_id)
    if not b or b["owner_id"] != cb.from_user.id or b["deleted"]:
        await cb.answer("Бот не найден", show_alert=True)
        return

    factory = _factory(ctx)
    if not factory or not factory.enabled:
        await cb.message.edit_text(
            "📢 <b>Заработок на рассылках</b>\n\n"
            "Система монетизации аудитории бота через платные рассылки.\n"
            "После подключения бот может получать оплачиваемые сообщения, "
            "а вы получаете долю от каждого размещения.\n\n"
            "Сервис пока не подключён в конструкторе. Обратитесь к администратору.",
            reply_markup=ikb([btn("⬅️ Назад", f"b:{bot_id}:open")]),
        )
        await cb.answer()
        return

    users = _users(ctx, bot_id)
    row = ctx.db.mgram_get(bot_id)

    if row:
        text = (
            f"📢 <b>Заработок на рассылках · бот ID {bot_id}</b>\n\n"
            f"Статус подключения: {html.escape(str(row.get('status') or '—'))}\n"
            f"Пользователей сейчас: {users}\n"
            f"Категория: {html.escape(str(row.get('category') or '—'))}\n\n"
            "Бот подключён к системе. Рассылки можно включать и настраивать "
            "в личном кабинете сервиса монетизации."
        )
        kb = ikb(
            [btn("🔄 Обновить статус", f"b:{bot_id}:mgram")],
            [btn("⬅️ Назад", f"b:{bot_id}:open")],
        )
    else:
        if users < MIN_USERS:
            text = (
                f"📢 <b>Заработок на рассылках · бот ID {bot_id}</b>\n\n"
                f"Пользователей в боте: <b>{users}</b>\n"
                f"Для подключения нужно минимум <b>{MIN_USERS}</b> пользователей.\n\n"
                "Когда аудитория вырастет — вернитесь сюда и подключите бота. "
                "После подключения вы сможете зарабатывать на платных рассылках."
            )
            kb = ikb([btn("⬅️ Назад", f"b:{bot_id}:open")])
        else:
            text = (
                f"📢 <b>Заработок на рассылках · бот ID {bot_id}</b>\n\n"
                f"Пользователей в боте: <b>{users}</b> — достаточно для подключения.\n\n"
                "Подключить бота к системе заработка на рассылках?\n\n"
                "После модерации бот сможет получать платные рассылки, "
                "а вы будете получать долю от каждого размещения. "
                "Токен бота передаётся только в систему монетизации."
            )
            kb = ikb(
                [btn("✅ Подключить", f"b:{bot_id}:mgram_reg")],
                [btn("⬅️ Назад", f"b:{bot_id}:open")],
            )

    await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await cb.answer()


@router.callback_query(F.data.startswith("b:") & F.data.endswith(":mgram_reg"))
async def mgram_register(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    bot_id = int(parts[1])
    b = ctx.db.bot(bot_id)
    if not b or b["owner_id"] != cb.from_user.id or b["deleted"]:
        await cb.answer("Бот не найден", show_alert=True)
        return
    factory = _factory(ctx)
    if not factory or not factory.enabled:
        await cb.answer("Сервис не подключён", show_alert=True)
        return

    users = _users(ctx, bot_id)
    if users < MIN_USERS:
        await cb.answer(
            f"Нужно минимум {MIN_USERS} пользователей. Сейчас: {users}.",
            show_alert=True,
        )
        return

    await cb.answer("Подключаю…")
    try:
        result = await factory.register_bot(b["token"], owner_id=b["owner_id"])
        data = result.get("data") or result
        ctx.db.mgram_save(
            bot_id,
            data.get("bot_id"),
            data.get("username"),
            data.get("status"),
            data.get("category"),
            api_key_shown=bool(data.get("api_key")),
        )
        api_key = data.get("api_key")
        extra = (
            f"\n\nКлюч доступа бота (показывается один раз): "
            f"<code>{html.escape(api_key)}</code>"
            if api_key else ""
        )
        text = (
            f"✅ Бот подключён к системе заработка на рассылках!\n\n"
            f"Статус: {html.escape(str(data.get('status') or 'на модерации'))}\n"
            f"Пользователей: {users}"
            + extra
        )
    except MgramError as exc:
        text = f"❌ Не удалось подключить: {html.escape(str(exc))}"
        if exc.code:
            text += f"\nКод: {html.escape(exc.code)}"
    finally:
        await factory.close()
    await cb.message.edit_text(
        text,
        reply_markup=ikb([btn("⬅️ Назад", f"b:{bot_id}:open")]),
        disable_web_page_preview=True,
    )
