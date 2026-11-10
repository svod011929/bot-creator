"""Шаблон «Реф. бот»: награда за приглашённых друзей, обязательная подписка, вывод."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import Message

from child.db import fmt
from child.referral_club import ReferralClub
from child.referral_views import BTN_CLUB
from child.referral_views import build_router as club_router
from child.templates import Setting, TemplateInfo
from child.withdrawals import is_owner

INFO = TemplateInfo(
    key="refbot",
    title="Реф. бот",
    emoji="👥",
    price_key="PRICE_REFBOT",
    description="Реферальный клуб с кампаниями, заданиями, подтверждёнными приглашениями, целями и достижениями. "
                "Клубные баллы учитываются отдельно от денежного баланса и ручных выплат.",
    settings=[
        Setting("welcome", "Приветствие", "👋 Приглашай друзей и участвуй в кампаниях! Открой «🚀 Клуб»: "
                "там знакомство, задания, цели и достижения. Денежные выплаты рассматриваются вручную по заявке."),
        Setting("club_title", "Название клуба", "Клуб амбассадоров"),
        Setting("club_intro", "Описание клуба", "Выбирай кампании, проходи задания и приглашай заинтересованных друзей. "
                "Клубные баллы отражают участие и не являются деньгами."),
        Setting("currency", "Валюта", "₽"),
        Setting("ref_reward", "Награда за друга", 5, "float"),
        Setting("min_withdraw", "Мин. вывод", 100, "float"),
        Setting("withdraw_details_prompt", "Текст запроса реквизитов", "Укажите номер карты или кошелька:"),
        Setting("required_channel", "Обязательный канал", "", "channel"),
        Setting("channel_url", "Ссылка на канал (для приватных)", "-"),
        Setting("about", "Текст «О боте»", "Приглашайте друзей по своей ссылке. Награда начисляется, "
                                        "когда друг подпишется на канал."),
    ],
    version=2,
    capabilities=("campaigns", "qualification", "club_points", "quiz", "goals"),
)

BTN_INVITE = "👥 Пригласить друзей"
BTN_TOP = "🏆 Топ рефоводов"
BTN_ABOUT = "ℹ️ О боте"
STATE_ESCAPE_TEXTS = (BTN_CLUB,)
ADMIN_BUTTONS = [("🚀 Кампании клуба", "club:admin:0")]


def create_club(app):
    return ReferralClub(app.db, owner_id=app.owner_id, owner_check=lambda uid: is_owner(app, uid))


def start_user(app, user, payload, *, private=True):
    if not private and payload.startswith("c_"):
        payload = ""
    return app.club.join(user.id, user.username, user.full_name, payload)


def on_access_confirmed(app, uid):
    app.club.confirm_access(uid)


def pause_forms(app, uid):
    if is_owner(app, uid):
        app.club.wizard_pause(uid)


def cancel_form(app, uid):
    if is_owner(app, uid):
        app.club.wizard_discard(uid)


def profile_extra(app, uid):
    s = app.club.summary(uid)
    return (f"\n\n🚀 <b>Клуб</b>\nКлубные баллы: {s['member']['points']} · подтверждённых приглашений: {s['qualified']}\n"
            "<i>Клубные баллы — прогресс участия; они не включены в баланс для вывода.</i>")


def admin_stats_extra(app):
    if not is_owner(app, app.owner_id):
        return ""
    s = app.club.stats(app.owner_id)
    return (f"\n\n🚀 <b>Клуб</b>\nУчастников: {s['members']} · активных кампаний: {s['active']} · черновиков: {s['drafts']}\n"
            f"Подтверждений: {s['qualified']} · выполненных заданий: {s['answers']}")


def menu_rows(app) -> list[list[str]]:
    return [[BTN_CLUB], [BTN_INVITE, BTN_TOP], [BTN_ABOUT]]


def build_router(app) -> Router:
    r = Router(name="refbot")

    @r.message(F.text == BTN_INVITE)
    async def invite(message: Message) -> None:
        pause_forms(app, message.from_user.id)
        uid = message.from_user.id
        await message.answer(
            f"👥 За каждого друга — <b>{fmt(float(app.s['ref_reward']))} {app.currency}</b>\n\n"
            f"Вы пригласили: {app.db.referrals(uid)}\n🔗 Ваша ссылка:\n{app.ref_link(uid)}\n\n"
            "Для целей, заданий и клубных баллов откройте «🚀 Клуб» и создайте отдельную ссылку кампании.",
            disable_web_page_preview=True,
        )

    @r.message(F.text == BTN_TOP)
    async def top(message: Message) -> None:
        pause_forms(app, message.from_user.id)
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
        pause_forms(app, message.from_user.id)
        await message.answer(html.escape(str(app.s["about"])) + app.footer())

    r.include_router(club_router(app))
    return r
