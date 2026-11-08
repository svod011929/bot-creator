"""📰 Информация и «Оповестить о проблеме»."""
from __future__ import annotations

import html
import time
from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from creator.keyboards import BTN_INFO, btn, cancel_kb, ikb, url_btn
from creator.services import Ctx
from creator.states import Info

router = Router(name="info")

SEP = "➖➖➖➖➖➖➖➖"


def days_running(ctx: Ctx) -> int:
    try:
        start = date.fromisoformat(ctx.cfg.launch_date) if ctx.cfg.launch_date else None
    except ValueError:
        start = None
    if start is None:
        start = date.fromtimestamp(int(ctx.db.kv_get("created_at") or time.time()))
    return max(1, (date.today() - start).days + 1)


def info_text(ctx: Ctx) -> str:
    s = ctx.db.stats()
    return (
        f"🗃 <b>Информация</b>\n{SEP}\n"
        f"⏱ <b>Работаем:</b> {days_running(ctx)} дн.\n{SEP}\n"
        f"▪️ <b>Пользователи:</b>\n"
        f"👤 Пользователей всего: {s['users_total']}\n"
        f"👤 Пользователей сегодня: {s['users_today']}\n{SEP}\n"
        f"▪️ <b>Боты:</b>\n"
        f"🤖 Ботов всего: {s['bots_total']}\n"
        f"🚀 Ботов запущено: {s['bots_running']}\n{SEP}"
    )


def info_kb(ctx: Ctx):
    links = []
    if ctx.cfg.chat_url:
        links.append(url_btn("💬 Чат", ctx.cfg.chat_url))
    if ctx.cfg.channel_url:
        links.append(url_btn("📢 Канал", ctx.cfg.channel_url))
    admin = url_btn("👑 Администратор", ctx.cfg.admin_url) if ctx.cfg.admin_url else btn("👑 Администратор", "info:admin")
    return ikb(links, [btn("⛔ Оповестить о проблеме", "info:report")], [admin])


@router.message(F.text == BTN_INFO)
async def info(message: Message, ctx: Ctx) -> None:
    await message.answer(info_text(ctx), reply_markup=info_kb(ctx))


@router.callback_query(F.data == "info:admin")
async def admin_contact(cb: CallbackQuery) -> None:
    await cb.answer("Напишите через «Оповестить о проблеме» — сообщение придёт администратору.", show_alert=True)


@router.callback_query(F.data == "info:report")
async def report(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Info.report)
    await cb.message.answer("⛔ Опишите проблему одним сообщением (можно с фото/скриншотом).", reply_markup=cancel_kb())
    await cb.answer()


@router.message(Info.report)
async def report_text(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    u = message.from_user
    text = message.text or message.caption or "(вложение)"
    rid = ctx.db.add_report(u.id, text)
    header = (f"⛔ <b>Обращение №{rid}</b> от {html.escape(u.full_name)} "
              f"(<code>{u.id}</code>{', @' + u.username if u.username else ''})")
    sent = 0
    for admin in ctx.cfg.admin_ids:
        try:
            await message.bot.send_message(admin, header)
            await message.forward(admin)
            sent += 1
        except Exception:  # noqa: BLE001
            pass
    await message.answer("✅ Спасибо! Сообщение передано администратору." if sent
                         else "✅ Сообщение сохранено, администратор его увидит.")
