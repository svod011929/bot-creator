"""Шаблон «Реф. бот»: награда за приглашённых друзей, обязательная подписка, вывод."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import Message

from child.db import fmt
from child.templates import Setting, TemplateInfo

INFO = TemplateInfo(
    key="refbot",
    title="Реф. бот",
    emoji="👥",
    price_key="PRICE_REFBOT",
    description="Пользователи приглашают друзей по реферальной ссылке и получают награду. "
                "Есть обязательная подписка на канал и заявки на вывод.",
    settings=[
        Setting("welcome", "Приветствие", "👋 Приглашай друзей и зарабатывай! Деньги выводятся по заявке."),
        Setting("currency", "Валюта", "₽"),
        Setting("ref_reward", "Награда за друга", 5, "float"),
        Setting("min_withdraw", "Мин. вывод", 100, "float"),
        Setting("withdraw_details_prompt", "Текст запроса реквизитов", "Укажите номер карты или кошелька:"),
        Setting("required_channel", "Обязательный канал", "", "channel"),
        Setting("channel_url", "Ссылка на канал (для приватных)", "-"),
        Setting("about", "Текст «О боте»", "Приглашайте друзей по своей ссылке. Награда начисляется, "
                                        "когда друг подпишется на канал."),
    ],
)

BTN_INVITE = "👥 Пригласить друзей"
BTN_TOP = "🏆 Топ рефоводов"
BTN_ABOUT = "ℹ️ О боте"


def menu_rows(app) -> list[list[str]]:
    return [[BTN_INVITE, BTN_TOP], [BTN_ABOUT]]


def build_router(app) -> Router:
    r = Router(name="refbot")

    @r.message(F.text == BTN_INVITE)
    async def invite(message: Message) -> None:
        uid = message.from_user.id
        await message.answer(
            f"👥 За каждого друга — <b>{fmt(float(app.s['ref_reward']))} {app.currency}</b>\n\n"
            f"Вы пригласили: {app.db.referrals(uid)}\n🔗 Ваша ссылка:\n{app.ref_link(uid)}",
            disable_web_page_preview=True,
        )

    @r.message(F.text == BTN_TOP)
    async def top(message: Message) -> None:
        rows = app.db.all(
            "SELECT r.referrer_id AS id, u.full_name AS name, COUNT(*) AS cnt FROM users r "
            "JOIN users u ON u.id=r.referrer_id WHERE r.referrer_id IS NOT NULL "
            "GROUP BY r.referrer_id ORDER BY cnt DESC LIMIT 10"
        )
        lines = ["🏆 <b>Топ рефоводов</b>\n"]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {html.escape(row['name'] or str(row['id']))} — {row['cnt']} 👥")
        if not rows:
            lines.append("Пока пусто — станьте первым!")
        await message.answer("\n".join(lines))

    @r.message(F.text == BTN_ABOUT)
    async def about(message: Message) -> None:
        await message.answer(html.escape(str(app.s["about"])) + app.footer())

    return r
