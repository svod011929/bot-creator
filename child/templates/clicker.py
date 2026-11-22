"""Шаблон «Кликер бот»: тап — монеты, улучшения силы клика, топ, вывод."""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.clicker_adventure import BTN_ADVENTURE, ranking_view
from child.clicker_adventure import build_router as adventure_router
from child.clicker_game import ClickerGame
from child.db import fmt
from child.templates import Setting, TemplateInfo

INFO = TemplateInfo(
    key="clicker",
    title="Кликер бот",
    emoji="⛏",
    price_key="PRICE_CLICKER",
    description="Кликер с экспедициями, стартовыми, ежедневными и недельными миссиями, тремя ветками навыков, "
                "коллекцией и рейтингом опыта. Баланс бота и игровой XP учитываются отдельно; выплаты — по ручным заявкам.",
    settings=[
        Setting("welcome", "Приветствие", "👋 Добро пожаловать в кликер! Жми «👆 Кликать» и зарабатывай монеты. "
                "В «🧭 Приключение» тебя ждут миссии, маршруты, навыки и коллекция находок."),
        Setting("adventure_title", "Название приключения", "Экспедиция"),
        Setting("adventure_intro", "Описание приключения", "Исследуй маршруты, собирай находки и выбирай навыки. "
                "Начни со стартовой миссии: пять кликов дают опыт для первого навыка."),
        Setting("currency", "Валюта", "🪙"),
        Setting("click_reward", "Монет за клик (ур. 1)", 1, "int"),
        Setting("upgrade_base_cost", "Цена улучшения (база)", 100, "int"),
        Setting("max_level", "Максимальный уровень", 50, "int"),
        Setting("ref_reward", "Награда за друга", 100, "float"),
        Setting("min_withdraw", "Мин. вывод", 10000, "float"),
        Setting("withdraw_details_prompt", "Текст запроса реквизитов", "Укажите реквизиты для выплаты:"),
        Setting("required_channel", "Обязательный канал", "", "channel"),
        Setting("channel_url", "Ссылка на канал (для приватных)", "-"),
    ],
    version=2,
    capabilities=("missions", "expeditions", "skills", "collection", "xp_ranking"),
)

BTN_CLICK = "👆 Кликать"
BTN_UPGRADE = "⬆️ Улучшения"
BTN_TOP = "🏆 Топ"
BTN_INVITE = "👥 Пригласить"

TAP_COOLDOWN = 0.25
STATE_ESCAPE_TEXTS = (BTN_ADVENTURE,)


def create_game(db):
    return ClickerGame(db)


def profile_extra(app, uid: int) -> str:
    p = app.game.overview(uid)["profile"]
    return (f"\n\n🧭 <b>Приключение</b>\nОбщий опыт: {p['xp_total']} XP · для навыков: {p['xp_available']} XP\n"
            f"Ранг: {1 + p['xp_total'] // 100} · экспедиций: {p['expeditions_total']}\n"
            "<i>XP используется для развития приключения и не выводится. Открыть игру: «🧭 Приключение».</i>")


def admin_stats_extra(app) -> str:
    s = app.game.stats()
    return (f"\n\n🧭 <b>Приключение</b>\nПрофилей: {s['players']} · участников рейтинга месяца: {s['season_players']}\n"
            f"Завершённых экспедиций: {s['expeditions']} · наград за миссии: {s['missions']}")


def menu_rows(app) -> list[list[str]]:
    return [[BTN_ADVENTURE], [BTN_CLICK, BTN_UPGRADE], [BTN_TOP, BTN_INVITE]]


def click_power(app, level: int) -> int:
    return int(app.s["click_reward"]) * level


def upgrade_cost(app, level: int) -> int:
    """Цена перехода с level на level+1."""
    return int(app.s["upgrade_base_cost"]) * level * level


def build_router(app) -> Router:
    r = Router(name="clicker")
    last_tap: dict[int, float] = {}

    def tap_view(u) -> tuple[str, InlineKeyboardMarkup]:
        power = click_power(app, u["level"])
        text = (f"💰 Баланс: <b>{fmt(u['balance'])} {app.currency}</b>\n"
                f"⚡ Сила клика: {power}\n👆 Кликов: {u['clicks']}")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"👆 ТАП (+{power})", callback_data="tap")
        ]])
        return text, kb

    @r.message(F.text == BTN_CLICK)
    async def click_screen(message: Message) -> None:
        u = app.db.user(message.from_user.id)
        if not u:
            return
        text, kb = tap_view(u)
        await message.answer(text, reply_markup=kb)

    @r.callback_query(F.data == "tap")
    async def tap(cb: CallbackQuery) -> None:
        uid = cb.from_user.id
        t = time.monotonic()
        if t - last_tap.get(uid, 0) < TAP_COOLDOWN:
            await cb.answer("Не так быстро 🙂")
            return
        last_tap[uid] = t
        u = app.db.user(uid)
        if not u:
            await cb.answer("Нажмите /start")
            return
        base = int(app.s["click_reward"])
        u = app.game.tap(uid, base_reward=base)
        power = base * u["level"]
        text, kb = tap_view(u)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await cb.answer(f"+{power}")

    def upgrade_view(u) -> tuple[str, InlineKeyboardMarkup | None]:
        lvl, max_lvl = u["level"], int(app.s["max_level"])
        text = f"⬆️ <b>Улучшения</b>\n\nУровень: {lvl}/{max_lvl}\nСила клика: {click_power(app, lvl)}"
        if lvl >= max_lvl:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🌿 Навыки за XP", callback_data="adv:skills")]])
            return text + "\n\n🏁 Максимальный уровень! Навыки приключения развиваются отдельно за XP.", kb
        cost = upgrade_cost(app, lvl)
        text += f"\nСледующий уровень: +{click_power(app, lvl + 1) - click_power(app, lvl)} к клику"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"Улучшить за {cost} {app.currency}", callback_data="upg")
        ], [InlineKeyboardButton(text="🌿 Выбрать навык за XP", callback_data="adv:skills")]])
        return text, kb

    @r.message(F.text == BTN_UPGRADE)
    async def upgrades(message: Message) -> None:
        u = app.db.user(message.from_user.id)
        if not u:
            return
        text, kb = upgrade_view(u)
        await message.answer(text, reply_markup=kb)

    @r.callback_query(F.data == "upg")
    async def do_upgrade(cb: CallbackQuery) -> None:
        u = app.db.user(cb.from_user.id)
        if not u or u["level"] >= int(app.s["max_level"]):
            await cb.answer("Максимальный уровень", show_alert=True)
            return
        cost = upgrade_cost(app, u["level"])
        if not app.game.upgrade_legacy(u["id"], level=u["level"], cost=cost, max_level=int(app.s["max_level"])):
            await cb.answer(f"Не хватает монет или уровень изменился: нужно {cost}. Откройте улучшения заново.",
                            show_alert=True)
            return
        text, kb = upgrade_view(app.db.user(u["id"]))
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await cb.answer("✅ Улучшено!")

    @r.message(F.text == BTN_TOP)
    async def top(message: Message) -> None:
        text, kb = ranking_view(app)
        await message.answer(text, reply_markup=kb)

    @r.message(F.text == BTN_INVITE)
    async def invite(message: Message) -> None:
        uid = message.from_user.id
        await message.answer(
            f"👥 Приглашайте друзей и получайте <b>{fmt(float(app.s['ref_reward']))} {app.currency}</b> за каждого!\n\n"
            f"Приглашено: {app.db.referrals(uid)}\n🔗 {app.ref_link(uid)}",
            disable_web_page_preview=True,
        )

    r.include_router(adventure_router(app))
    return r
