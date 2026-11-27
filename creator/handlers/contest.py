"""Конкурсы: просмотр таблицы и управление из админки."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from creator.contest import leaderboard_text
from creator.keyboards import btn, cancel_kb, ikb
from creator.services import Ctx
from creator.states import Admin
from creator.ui import show

router = Router(name="contest")


def contest_kb(is_admin: bool = False) -> object:
    rows = []
    if is_admin:
        rows.append([btn("➕ Создать конкурс", "a:contest:new")])
        rows.append([btn("🏁 Завершить текущий", "a:contest:end")])
    rows.append([btn("🔄 Обновить", "contest:view")])
    rows.append([btn("⬅️ Назад", "a:main" if is_admin else "info:back")])
    return ikb(*rows)


@router.callback_query(F.data == "contest:view")
async def view(cb: CallbackQuery, ctx: Ctx) -> None:
    text = leaderboard_text(ctx, cb.from_user.id)
    is_admin = ctx.is_admin(cb.from_user.id)
    await show(cb, text, contest_kb(is_admin))
    await cb.answer()


@router.callback_query(F.data == "a:contest:new")
async def new_start(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    if not ctx.is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(Admin.contest)
    await state.update_data(contest_step="title")
    await cb.message.answer("Название конкурса:", reply_markup=cancel_kb("a:main"))
    await cb.answer()


@router.message(Admin.contest, F.text)
async def new_input(message: Message, state: FSMContext, ctx: Ctx) -> None:
    if not ctx.is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    step = data.get("contest_step")
    if step not in {"title", "prize", "days"}:
        return
    text = (message.text or "").strip()
    if step == "title":
        if not text:
            await message.answer("Введите название конкурса.")
            return
        await state.update_data(contest_title=text, contest_step="prize")
        await message.answer("Описание призового фонда (например: 3 000 ₽ · 1 500 / 1 000 / 500):")
    elif step == "prize":
        if not text:
            await message.answer("Введите описание призового фонда.")
            return
        await state.update_data(contest_prize=text, contest_step="days")
        await message.answer("Длительность в днях (число):")
    elif step == "days":
        try:
            days = int(text)
            if days < 1 or days > 365:
                raise ValueError
        except ValueError:
            await message.answer("Введите число дней от 1 до 365.")
            return
        title = data.get("contest_title") or "Конкурс"
        prize = data.get("contest_prize") or "Призы"
        cid = ctx.db.contest_create(title, prize, days)
        await state.clear()
        await message.answer(
            f"✅ Конкурс создан (ID {cid}).\n\n" + leaderboard_text(ctx, message.from_user.id),
            reply_markup=contest_kb(True),
        )


@router.callback_query(F.data == "a:contest:end")
async def end(cb: CallbackQuery, ctx: Ctx) -> None:
    if not ctx.is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    c = ctx.db.contest_active()
    if not c:
        await cb.answer("Нет активного конкурса", show_alert=True)
        return
    ctx.db.contest_end(c["id"])
    await cb.message.edit_text("Конкурс завершён.", reply_markup=ikb([btn("⬅️ В панель", "a:main")]))
    await cb.answer()
