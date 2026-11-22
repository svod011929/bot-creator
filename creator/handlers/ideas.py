"""Идеи и обратная связь: голосование и награды за принятые предложения."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from creator.keyboards import btn, cancel_kb, ikb
from creator.services import Ctx

router = Router(name="ideas")

REWARD_KOP = 5000  # 50 ₽ за принятую идею


class IdeaForm(StatesGroup):
    text = State()


@router.callback_query(F.data == "ideas")
@router.message(F.text == "💡 Идеи")
async def ideas_home(event: Message | CallbackQuery, ctx: Ctx) -> None:
    items = ctx.db.idea_list(8)
    lines = ["💡 <b>Идеи по развитию конструктора</b>\n"]
    if not items:
        lines.append("Пока нет идей. Будьте первым!")
    else:
        for it in items:
            status = {"new": "🆕", "accepted": "✅", "done": "🎉"}.get(it["status"], "•")
            lines.append(f"{status} #{it['id']} · {it['votes']} голосов\n{html.escape(it['text'][:120])}")
    text = "\n\n".join(lines)
    rows = [[btn("➕ Предложить идею", "ideas:new")]]
    for it in items[:5]:
        rows.append([btn(f"👍 #{it['id']} ({it['votes']})", f"ideas:vote:{it['id']}")])
    rows.append([btn("⬅️ Назад", "info:back")])
    markup = ikb(*rows)
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.edit_text(text, reply_markup=markup)
    else:
        await event.answer(text, reply_markup=markup)


@router.callback_query(F.data == "ideas:new")
async def ideas_new(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(IdeaForm.text)
    await cb.message.answer(
        "Опишите идею одним сообщением (до 1000 символов).\n"
        "За принятую идею начисляется 50 ₽ на баланс.",
        reply_markup=cancel_kb(),
    )
    await cb.answer()


@router.message(IdeaForm.text)
async def ideas_save(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    text = (message.text or "").strip()
    if len(text) < 10:
        await message.answer("Слишком коротко. Опишите подробнее.")
        return
    iid = ctx.db.idea_add(message.from_user.id, text)
    await message.answer(
        f"✅ Идея #{iid} сохранена!\n\n"
        "Другие пользователи смогут за неё голосовать. "
        "Если идея будет принята — вы получите 50 ₽ на баланс.",
        reply_markup=ikb([btn("💡 К идеям", "ideas")]),
    )


@router.callback_query(F.data.startswith("ideas:vote:"))
async def ideas_vote(cb: CallbackQuery, ctx: Ctx) -> None:
    iid = int(cb.data.split(":")[2])
    if ctx.db.idea_vote(iid, cb.from_user.id):
        await cb.answer("Голос учтён!")
    else:
        await cb.answer("Вы уже голосовали за эту идею", show_alert=True)
    # refresh
    await ideas_home(cb, ctx)
