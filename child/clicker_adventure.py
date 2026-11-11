"""Telegram screens for the clicker adventure; amounts and deadlines always come from the server."""
from __future__ import annotations

import html
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.clicker_game import (
    ACHIEVEMENTS,
    ENERGY_INTERVAL,
    ENERGY_MAX,
    GAME_TZ,
    ITEMS,
    MISSIONS,
    ROUTES,
    SKILLS,
    GameError,
    skill_cost,
)
from child.db import fmt

BTN_ADVENTURE = "🧭 Приключение"
SCOPES = {"story": "Старт", "day": "Сегодня", "week": "Неделя"}


def button(title: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=title, callback_data=data)


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def duration(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds} сек."
    if seconds < 3600:
        return f"{seconds // 60} мин. {seconds % 60} сек."
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин."


def back() -> list[InlineKeyboardButton]:
    return [button("🧭 Приключение", "adv:home")]


def home(app, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    state = app.game.overview(uid)
    p, trip, now = state["profile"], state["expedition"], state["now"]
    total = p["xp_total"]
    chapter = ("Первые шаги", "Лесная станция", "Тайны руин", "Карта звёзд")[
        sum(total >= n for n in (40, 140, 360))
    ]
    title = html.escape(str(app.s.get("adventure_title", "Экспедиция"))[:80])
    intro = html.escape(str(app.s.get("adventure_intro", "Исследуй маршруты, собирай находки и выбирай навыки."))[:600])
    lines = [f"🧭 <b>{title}</b>", intro, "", f"📖 Глава: <b>{chapter}</b>",
             f"🏅 Ранг: {1 + total // 100} · до следующего {100 - total % 100} XP",
             f"✨ Общий опыт: <b>{total} XP</b> · для навыков: <b>{p['xp_available']} XP</b>",
             f"⚡ Энергия: {p['energy']}/{ENERGY_MAX}",
             f"🔥 Серия активности: {p['active_streak']} дн. · рекорд {p['best_streak']}",
             f"🎒 Экспедиций: {p['expeditions_total']} · находок: {len(state['inventory'])}/{len(ITEMS)}"]
    if p["energy"] < ENERGY_MAX:
        lines.append(f"Следующая энергия через {duration(p['energy_updated_at'] + ENERGY_INTERVAL - now)}.")
    rows = []
    if trip:
        route = ROUTES[trip["route_key"]]
        if trip["ready_at"] <= now:
            lines.extend(("", f"✅ {route.title}: находка ждёт получения!"))
            rows.append([button("🎒 Забрать находку и опыт", f"adv:claim:{trip['id']}")])
        else:
            lines.extend(("", f"🚶 {route.title}: осталось {duration(trip['ready_at'] - now)}."))
    else:
        lines.extend(("", "Первый маршрут занимает одну минуту. Пока идёт экспедиция, выполняй миссии."))
    missions = app.game.missions(uid, "story")
    pending = next((m for m in missions if not m["claimed"]), None)
    if pending:
        progress = min(pending["progress"], pending["target"])
        lines.extend(("", f"🎯 Следующая цель: {pending['title']} · {progress}/{pending['target']}"))
    rows += [[button("🎯 Миссии", "adv:missions:story"), button("🗺 Маршруты", "adv:routes")],
             [button("🌿 Навыки", "adv:skills"), button("🎒 Коллекция", "adv:collection")],
             [button("🎉 События и значки", "evt:home:0")],
             [button("🏆 Рейтинг", "adv:rank:month"), button("🔄 Обновить", "adv:home")]]
    lines.extend(("", "<i>XP — игровой опыт для навыков и рейтинга. Он не расходует баланс бота и не выводится.</i>"))
    return "\n".join(lines), keyboard(rows)


def missions_view(app, uid: int, scope: str) -> tuple[str, InlineKeyboardMarkup]:
    if scope not in SCOPES:
        raise GameError("Выберите период миссий заново.")
    missions = app.game.missions(uid, scope)
    lines = [f"🎯 <b>Миссии · {SCOPES[scope]}</b>", "Награды дают игровой XP. Забирай их после выполнения."]
    rows = [[button(("• " if key == scope else "") + title, f"adv:missions:{key}") for key, title in SCOPES.items()]]
    for m in missions:
        ready = m["progress"] >= m["target"]
        status = "✅ Получено" if m["claimed"] else "🎁 Можно забрать" if ready else "⏳ В процессе"
        lines.extend(("", f"<b>{m['title']}</b>",
                      f"{min(m['progress'], m['target'])}/{m['target']} · +{m['xp']} XP · {status}"))
        if ready and not m["claimed"]:
            rows.append([button(f"🎁 Забрать {m['xp']} XP · {m['title'][:26]}", f"adv:mission:{m['key']}:{m['period']}")])
    if scope == "day":
        lines.extend(("", "Ежедневные миссии обновляются в 00:00 МСК."))
    elif scope == "week":
        lines.extend(("", "Недельные миссии обновляются в понедельник в 00:00 МСК."))
    else:
        lines.extend(("", "Стартовые миссии выполняются один раз. Дальше доступны ежедневные и недельные цели."))
    rows += [[button("🗺 Маршруты", "adv:routes"), button("🌿 Навыки", "adv:skills")], back()]
    return "\n".join(lines), keyboard(rows)


def routes_view(app, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    state = app.game.overview(uid)
    p, trip, now = state["profile"], state["expedition"], state["now"]
    rows = []
    lines = ["🗺 <b>Маршруты</b>", f"⚡ Энергия: {p['energy']}/{ENERGY_MAX}; +1 каждые 20 минут."]
    if trip:
        route = ROUTES[trip["route_key"]]
        lines.extend(("", f"🚶 <b>{route.title}</b>", f"Награда: {trip['reward_xp']} XP и {ITEMS[trip['item_key']]}",
                      f"Возвращение: {datetime.fromtimestamp(trip['ready_at'], GAME_TZ):%d.%m %H:%M:%S} МСК"))
        if trip["ready_at"] <= now:
            lines.append("✅ Маршрут завершён. Забери результат, чтобы отправиться снова.")
            rows.append([button("🎒 Забрать результат", f"adv:claim:{trip['id']}")])
        else:
            lines.append(f"Осталось {duration(trip['ready_at'] - now)}. Миссии и клики доступны во время ожидания.")
        rows += [[button("🔄 Проверить", "adv:routes"), button("🎯 Миссии", "adv:missions:day")], back()]
        return "\n".join(lines), keyboard(rows)
    for route in ROUTES.values():
        seconds = max(10, route.duration * (100 - 10 * p["navigation"]) // 100)
        xp = route.xp * (100 + 10 * p["equipment"]) // 100
        unlocked = p["xp_total"] >= route.min_xp and p["research"] >= route.research
        lines.extend(("", f"<b>{route.title}</b>", route.description,
                      f"⏱ {duration(seconds)} · ⚡ {route.energy} · +{xp} XP · {ITEMS[route.item]}"))
        if not unlocked:
            lines.append(f"🔒 Нужно {route.min_xp} общего XP и исследование {route.research} ур.")
        elif p["energy"] < route.energy:
            lines.append("⏳ Дождись восстановления энергии.")
        else:
            rows.append([button(f"🚶 {route.title}", f"adv:go:{route.key}:{p['version']}")])
    lines.extend(("", "Навигация сокращает время, снаряжение увеличивает XP. Их эффект фиксируется при старте."))
    rows += [[button("🌿 Развить навыки", "adv:skills")], back()]
    return "\n".join(lines), keyboard(rows)


def skills_view(app, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    p = app.game.overview(uid)["profile"]
    lines = ["🌿 <b>Выбери направление развития</b>", f"Доступно: <b>{p['xp_available']} XP</b>. Общий опыт: {p['xp_total']} XP.",
             "Навыки покупаются за опыт приключения. Место в рейтинге после покупки сохраняется."]
    rows = []
    for skill in SKILLS.values():
        level = p[skill.key]
        lines.extend(("", f"<b>{skill.title} · {level}/{skill.max_level}</b>", skill.description))
        if level >= skill.max_level:
            lines.append("✅ Развито до максимума.")
            continue
        cost = skill_cost(level)
        lines.append(f"Следующий уровень: {cost} XP.")
        if cost <= p["xp_available"]:
            rows.append([button(f"{skill.title} · улучшить за {cost} XP", f"adv:skill:{skill.key}:{p['version']}")])
    lines.extend(("", "Опыт дают миссии и завершённые экспедиции. Улучшение силы клика остаётся в «⬆️ Улучшения»."))
    rows += [[button("🎯 Получить опыт", "adv:missions:story")], back()]
    return "\n".join(lines), keyboard(rows)


def collection_view(app, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    state = app.game.overview(uid)
    found = {r["item_key"]: r["count"] for r in state["inventory"]}
    badges = {r["key"] for r in state["achievements"]}
    lines = ["🎒 <b>Коллекция и достижения</b>", "На каждом маршруте есть своя находка. Повторы сохраняются в коллекции."]
    for key, title in ITEMS.items():
        lines.append(f"{title} · {found.get(key, 0)} шт." if key in found else f"🔒 {title} · ещё не найдено")
    lines.extend(("", "🏅 <b>Достижения</b>"))
    for key, title in ACHIEVEMENTS.items():
        lines.append(("✅ " if key in badges else "▫️ ") + title)
    lines.extend(("", "📜 <b>Последние изменения опыта</b>"))
    for row in state["history"]:
        title = {"mission": "Миссия", "expedition": "Экспедиция", "skill": "Навык"}[row["kind"]]
        if row["source_key"].startswith("event:"):
            title = "Событие"
        lines.append(f"{title}: {row['delta']:+d} XP · {datetime.fromtimestamp(row['created_at'], GAME_TZ):%d.%m %H:%M} МСК")
    if not state["history"]:
        lines.append("Первую награду можно получить за пять кликов в стартовой миссии.")
    return "\n".join(lines), keyboard([[button("🗺 Маршруты", "adv:routes"), button("🎯 Миссии", "adv:missions:story")], back()])


def ranking_view(app, scope: str = "month") -> tuple[str, InlineKeyboardMarkup]:
    rows = app.game.leaderboard(scope)
    titles = {"month": "Опыт за месяц", "all": "Опыт за всё время", "balance": "Баланс бота"}
    lines = [f"🏆 <b>Топ игроков · {titles[scope]}</b>"]
    for index, row in enumerate(rows, 1):
        name = html.escape((row["full_name"] or "Игрок")[:50])
        amount = f"{fmt(row['score'])} {html.escape(app.currency)}" if scope == "balance" else f"{row['score']} XP"
        lines.append(f"{index}. {name} — {amount}")
    if not rows:
        lines.append("Пока нет результатов. Выполни стартовую миссию и открой рейтинг!")
    if scope != "balance":
        lines.extend(("", "Траты XP на навыки не уменьшают результат. Месячный рейтинг обновляется в начале месяца по МСК; "
                      "общий опыт и навыки сохраняются."))
    nav = [[button("Месяц", "adv:rank:month"), button("Всё время", "adv:rank:all"), button("Баланс", "adv:rank:balance")], back()]
    return "\n".join(lines), keyboard(nav)


def integer(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
        raise GameError("Кнопка устарела. Откройте приключение заново.")
    return int(raw)


def build_router(app) -> Router:
    r = Router(name="clicker-adventure")

    @r.message(F.text == BTN_ADVENTURE)
    async def adventure(message: Message, state: FSMContext) -> None:
        if message.chat.type != "private":
            await message.answer("Откройте приключение в личном чате с ботом.")
            return
        try:
            text, kb = home(app, message.from_user.id)
        except GameError as exc:
            await message.answer(str(exc))
            return
        await state.clear()
        await message.answer(text, reply_markup=kb)

    @r.callback_query(F.data.startswith("adv:"))
    async def action(cb: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cb.message, Message) or cb.message.chat.type != "private" or cb.message.chat.id != cb.from_user.id:
            await cb.answer("Откройте приключение в личном чате с ботом.", show_alert=True)
            return
        uid, note = cb.from_user.id, ""
        parts = cb.data.split(":")
        try:
            if cb.data == "adv:home":
                text, kb = home(app, uid)
            elif cb.data == "adv:routes":
                text, kb = routes_view(app, uid)
            elif cb.data == "adv:skills":
                text, kb = skills_view(app, uid)
            elif cb.data == "adv:collection":
                text, kb = collection_view(app, uid)
            elif len(parts) == 3 and parts[1] == "missions":
                text, kb = missions_view(app, uid, parts[2])
            elif len(parts) == 3 and parts[1] == "rank":
                text, kb = ranking_view(app, parts[2])
            elif len(parts) == 4 and parts[1] == "go":
                app.game.start_expedition(uid, parts[2], integer(parts[3]))
                note = "🚶 Экспедиция началась!"
                text, kb = routes_view(app, uid)
            elif len(parts) == 4 and parts[1] == "skill":
                app.game.learn_skill(uid, parts[2], integer(parts[3]))
                note = "✅ Навык улучшен!"
                text, kb = skills_view(app, uid)
            elif len(parts) == 4 and parts[1] == "mission":
                xp = app.game.claim_mission(uid, parts[2], parts[3])
                note = f"🎁 +{xp} XP"
                text, kb = missions_view(app, uid, MISSIONS[parts[2]].scope)
            elif len(parts) == 3 and parts[1] == "claim":
                reward = app.game.claim_expedition(uid, integer(parts[2]))
                note = f"🎒 +{reward['xp']} XP · {ITEMS[reward['item']]}"
                text, kb = home(app, uid)
            else:
                raise GameError("Кнопка устарела. Откройте приключение заново.")
        except GameError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await cb.answer(note)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                await cb.message.answer(text, reply_markup=kb)

    return r
