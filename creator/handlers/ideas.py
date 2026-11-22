"""Идеи и обратная связь: голосование и награды за принятые предложения."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from creator.keyboards import btn, cancel_kb, ikb
from creator.money import rub
from creator.services import Ctx

router = Router(name="ideas")

REWARD_KOP = 5000  # значение по умолчанию: 50 ₽
IDEA_REWARD_MAX_KOP = 1_000_000  # 10 000 ₽


def idea_reward_kop(ctx: Ctx) -> int:
    """Текущая награда в копейках; без настройки — 50 ₽, не выше безопасного максимума."""
    raw = ctx.db.kv_get("idea_reward_kop")
    try:
        value = int(raw) if raw not in (None, "") else REWARD_KOP
    except (TypeError, ValueError):
        value = REWARD_KOP
    return max(0, min(value, IDEA_REWARD_MAX_KOP))


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
        await event.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    else:
        await event.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "ideas:new")
async def ideas_new(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await state.set_state(IdeaForm.text)
    reward = idea_reward_kop(ctx)
    extra = f"\nЗа принятую идею начисляется {rub(reward)} на баланс." if reward else ""
    await cb.message.answer(
        "Опишите идею одним сообщением (до 1000 символов)." + extra,
        reply_markup=cancel_kb(),
        disable_web_page_preview=True,
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
    reward = idea_reward_kop(ctx)
    bonus = f" Если идея будет принята — вы получите {rub(reward)} на баланс." if reward else ""
    await message.answer(
        f"✅ Идея #{iid} сохранена!\n\n"
        "Другие пользователи смогут за неё голосовать." + bonus,
        reply_markup=ikb([btn("💡 К идеям", "ideas")]),
        disable_web_page_preview=True,
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
