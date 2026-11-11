"""Player events and owner screens reused by the Creator's persistent editor."""

from __future__ import annotations

import html
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.clicker_events import FIELDS, EventError, date_value
from child.clicker_game import GAME_TZ, period_key
from child.db import fmt

BTN_EVENTS = "🎉 События"
LABELS = ("Название", "Описание", "Начало", "Дней", "Клики", "Экспедиции", "Маршруты", "Награда XP", "Значок")
PROMPTS = {
    "title": "Название события: от 1 до 80 символов.",
    "description": "Описание и история события: от 1 до 900 символов.",
    "starts": "Дата начала: ГГГГ-ММ-ДД или «сегодня». До года вперёд, по Москве (UTC+3).",
    "days": "Срок: от 1 до 30 календарных дней, включая день начала.",
    "taps": "Цель по новым кликам: от 0 до 100000. Ноль отключает эту цель.",
    "expeditions": "Сколько новых экспедиций завершить: от 0 до 1000. Ноль отключает цель.",
    "routes": "Сколько разных маршрутов пройти: от 0 до 4. Ноль отключает цель.",
    "reward": "Награда: от 1 до 1000 игрового XP. XP используется для навыков и рейтинга.",
    "badge": "Название памятного значка: от 1 до 60 символов, можно с эмодзи.",
}


def e(value, limit=1000):
    return html.escape(str(value)[:limit])


def button(title, data):
    return InlineKeyboardButton(text=title, callback_data=data)


def markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def date_label(day):
    return date_value(day).strftime("%d.%m.%Y")


def phase(c, at):
    if c["status"] == "draft":
        return "📝 Черновик"
    today = period_key("day", at)
    if today > c["end_day"]:
        return "🏁 Завершено"
    if c["status"] == "paused":
        return "⏸ Вступление на паузе"
    return "📅 Скоро" if today < c["start_day"] else "🟢 Идёт"


def event_text(c):
    end = c["end_day"] or (date_value(c["start_day"]) + timedelta(days=c["days"] - 1)).strftime("%Y%m%d")
    lines = [
        f"🎉 <b>{e(c['title'], 80)}</b>",
        e(c["description"], 900),
        "",
        f"📅 {date_label(c['start_day'])} — {date_label(end)} · МСК",
        "🎯 <b>Выполни все включённые цели</b>",
    ]
    for key, label in (("taps", "Новые клики"), ("expeditions", "Новые экспедиции"), ("routes", "Разные маршруты")):
        if c[key]:
            lines.append(f"• {label}: {c[key]}")
    lines += [
        f"🎁 {c['reward']} игрового XP · {e(c['badge'], 60)}",
        "",
        "Прогресс считается после вступления. Экспедиции должны быть начаты и получены после вступления, "
        "а результат — получен до конца события. Повторы одного маршрута считаются в экспедициях, "
        "но не увеличивают число разных маршрутов.",
        "Забрать награду за выполненные в срок цели можно ещё 7 дней после окончания. "
        "XP даёт навыки и место в рейтинге; денежная награда за событие не начисляется.",
    ]
    return "\n".join(lines)


def admin_list(app, uid, page):
    data = app.events.list_events(uid, page, owner=True)
    lines = ["🎉 <b>События кликера</b>", f"Страница {data['page'] + 1} · всего {data['total']}"]
    rows = []
    for c in data["rows"]:
        lines += [
            "",
            f"<b>{e(c['title'], 80)}</b> · {phase(c, app.events._now())}",
            f"С {date_label(c['start_day'])} · {c['days']} дн. · {c['reward']} XP",
        ]
        rows.append([button(c["title"][:55], f"evt:manage:{c['id']}")])
    nav = []
    if data["page"]:
        nav.append(button("⬅️", f"evt:admin:{data['page'] - 1}"))
    if (data["page"] + 1) * 5 < data["total"]:
        nav.append(button("➡️", f"evt:admin:{data['page'] + 1}"))
    rows += [nav, [button("➕ Новое событие", "evt:new")], [button("📚 Контент бота", "evt:home")]]
    return "\n".join(lines), markup(rows)


def owner_event_view(app, uid, cid, confirm=False):
    c = app.events.owner_event(uid, cid)
    text = event_text(c) + f"\n\n{phase(c, app.events._now())} · версия {c['revision']}\n"
    text += f"Участников: {c['participants']} · забрали награду: {c['claimed']}"
    rows = []
    if c["status"] == "draft":
        if confirm:
            text += "\n\nОпубликовать? Условия закрепятся. Пересечение дат с другим событием не допускается."
            rows.append([button("✅ Опубликовать", f"evt:publish:{cid}:{c['revision']}")])
        else:
            rows += [
                [button("✏️ Изменить", f"evt:edit:{cid}:{c['revision']}")],
                [button("📢 Публикация", f"evt:publish_view:{cid}:{c['revision']}")],
            ]
    else:
        action, title = ("pause", "⏸ Приостановить вступление") if c["status"] == "active" else ("activate", "▶️ Возобновить")
        if c["end_day"] >= period_key("day", app.events._now()):
            rows.append([button(title, f"evt:{action}:{cid}:{c['revision']}")])
        text += "\nПауза закрывает вступление. Участники продолжают выполнять цели до срока."
    rows += [
        [button("📄 Создать копию", f"evt:copy:{cid}")],
        [button("⬅️ Каталог", "evt:admin:0")],
        [button("📚 Контент бота", "evt:home")],
    ]
    return text, markup(rows)


def editor_view(app, uid):
    form = app.events.editor_get(uid)
    if not form:
        raise EventError("Откройте новую или сохранённую форму в креаторе.")
    data, step = form["data"], form["step"]
    rows = []
    if step == "confirm":
        text = "📝 <b>Проверь событие</b>\n" + "\n".join(
            f"<b>{label}:</b> {e(data[field], 900)}" for field, label in zip(FIELDS, LABELS, strict=True)
        )
        text += "\n\nСохранение создаёт черновик. Затем проверь предпросмотр и опубликуй его."
        rows.append([button("💾 Сохранить черновик", f"evt:save:{form['version']}")])
        for index in range(0, len(FIELDS), 3):
            rows.append(
                [
                    button(label, f"evt:field:{field}:{form['version']}")
                    for field, label in list(zip(FIELDS, LABELS, strict=True))[index : index + 3]
                ]
            )
    else:
        text = (
            f"📝 <b>Событие · шаг {FIELDS.index(step) + 1}/{len(FIELDS)}</b>\n{PROMPTS[step]}\n\nОтправьте значение сообщением."
        )
    rows += [[button("⏳ Продолжить позже", "evt:later")], [button("🗑 Удалить форму", "evt:discard")]]
    return text, markup(rows)


def player_list(app, uid, page=0):
    data = app.events.list_events(uid, page)
    lines = [
        "🎉 <b>События</b>",
        "Тематические испытания, игровой опыт и памятные значки.",
        f"Страница {data['page'] + 1} · событий: {data['total']}",
    ]
    rows = []
    for c in data["rows"]:
        lines += [
            "",
            f"<b>{e(c['title'], 80)}</b> · {phase(c, app.events._now())}",
            f"{date_label(c['start_day'])} — {date_label(c['end_day'])} · {c['reward']} XP",
        ]
        rows.append([button(c["title"][:55], f"evt:event:{c['id']}")])
    if not data["rows"]:
        lines.append("\nВладелец ещё не опубликовал события. Пока можно исследовать маршруты и выполнять миссии.")
    nav = []
    if data["page"]:
        nav.append(button("⬅️", f"evt:home:{data['page'] - 1}"))
    if (data["page"] + 1) * 5 < data["total"]:
        nav.append(button("➡️", f"evt:home:{data['page'] + 1}"))
    rows += [nav, [button("🏅 Мои значки", "evt:awards:0")], [button("🧭 Приключение", "adv:home")]]
    return "\n".join(lines), markup(rows)


def player_view(app, uid, cid):
    c = app.events.player_event(uid, cid)
    now, p = app.events._now(), c["player"]
    text = event_text(c) + f"\n\n{phase(c, now)}"
    rows = []
    if p:
        text += "\n\n<b>Твой прогресс</b>"
        for key, label in (("taps", "Клики"), ("expeditions", "Экспедиции"), ("routes", "Разные маршруты")):
            if c[key]:
                text += f"\n{label}: {min(c['progress'][key], c[key])}/{c[key]}"
        if p["reward_claimed_at"] is not None:
            text += f"\n✅ Награда получена: {p['reward_xp']} XP · {e(p['badge_title'], 60)}"
        elif now >= c["claim_until"]:
            text += "\n⌛ Срок получения награды закончился."
        elif c["ready"]:
            text += "\n🎁 Все цели выполнены! Забери награду."
            rows.append([button(f"🎁 Забрать {c['reward']} XP и значок", f"evt:claim:{cid}")])
        elif now >= c["ends_at"]:
            text += "\n🏁 Время вышло. Выполнены не все цели."
        else:
            text += "\n⏳ Продолжай, пока событие идёт."
        text += f"\nНаграда доступна до {datetime.fromtimestamp(c['claim_until'], GAME_TZ):%d.%m.%Y %H:%M} МСК."
    elif c["status"] == "active" and c["start_day"] <= period_key("day", now) <= c["end_day"]:
        text += "\n\nВступи, чтобы начать считать личный прогресс."
        rows.append([button("🚀 Участвовать", f"evt:join:{cid}:{c['revision']}")])
    rows += [
        [button("👆 Кликать", "evt:tap"), button("🗺 Маршруты", "adv:routes")],
        [button("🏆 Первые победители", f"evt:rank:{cid}"), button("🔄 Обновить", f"evt:event:{cid}")],
        [button("🎉 Все события", "evt:home:0")],
    ]
    return text, markup(rows)


def ranking(app, cid):
    lines = ["🏆 <b>Первые получившие награду</b>", "Порядок по времени получения награды; при совпадении — по ID игрока."]
    for index, row in enumerate(app.events.leaderboard(cid), 1):
        lines.append(f"{index}. {e(row['full_name'] or 'Игрок', 50)} · {row['reward_xp']} XP")
    if len(lines) == 2:
        lines.append("Пока никто не забрал награду.")
    return "\n".join(lines), markup([[button("⬅️ Событие", f"evt:event:{cid}")]])


def awards(app, uid, page):
    data = app.events.awards(uid, page)
    lines = ["🏅 <b>Мои значки событий</b>", f"Страница {data['page'] + 1} · значков: {data['total']}"]
    for c in data["rows"]:
        lines += ["", f"<b>{e(c['badge_title'], 60)}</b>", f"{e(c['title'], 80)} · {c['reward_xp']} XP"]
    if not data["rows"]:
        lines.append("\nПервый значок появится после выполнения события и получения награды.")
    nav = []
    if data["page"]:
        nav.append(button("⬅️", f"evt:awards:{data['page'] - 1}"))
    if (data["page"] + 1) * 8 < data["total"]:
        nav.append(button("➡️", f"evt:awards:{data['page'] + 1}"))
    return "\n".join(lines), markup([nav, [button("🎉 Все события", "evt:home:0")]])


def number(raw):
    if not raw.isascii() or not raw.isdigit() or len(raw) > 19 or int(raw) > 2**63 - 1:
        raise EventError("Кнопка устарела. Откройте события заново.")
    return int(raw)


def private(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    return isinstance(message, Message) and message.chat.type == "private" and message.chat.id == event.from_user.id


def build_router(app):
    router = Router(name="clicker-events")

    @router.message(F.text == BTN_EVENTS)
    async def entry(message: Message, state: FSMContext):
        if not private(message):
            await message.answer("Откройте события в личном чате с ботом.")
            return
        await state.clear()
        text, kb = player_list(app, message.from_user.id)
        await message.answer(text, reply_markup=kb)

    @router.callback_query(F.data.startswith("evt:"))
    async def action(cb: CallbackQuery, state: FSMContext):
        if not private(cb):
            await cb.answer("Откройте события в личном чате с ботом.", show_alert=True)
            return
        uid, note = cb.from_user.id, ""
        parts = cb.data.split(":")
        try:
            if len(parts) == 3 and parts[1] == "home":
                screen = player_list(app, uid, number(parts[2]))
            elif len(parts) == 3 and parts[1] == "event":
                screen = player_view(app, uid, number(parts[2]))
            elif len(parts) == 4 and parts[1] == "join":
                cid, revision = number(parts[2]), number(parts[3])
                app.events.join(uid, cid, revision)
                note, screen = "🚀 Ты в событии!", player_view(app, uid, cid)
            elif len(parts) == 3 and parts[1] == "claim":
                cid = number(parts[2])
                reward = app.events.claim(uid, cid)
                note, screen = f"🎁 +{reward} XP и значок!", player_view(app, uid, cid)
            elif len(parts) == 3 and parts[1] == "rank":
                screen = ranking(app, number(parts[2]))
            elif len(parts) == 3 and parts[1] == "awards":
                screen = awards(app, uid, number(parts[2]))
            elif cb.data == "evt:tap":
                user = app.db.user(uid)
                if not user:
                    raise EventError("Сначала нажмите /start.")
                power = int(app.s["click_reward"]) * user["level"]
                text = (
                    f"💰 Баланс: <b>{fmt(user['balance'])} {e(app.currency, 30)}</b>\n"
                    f"⚡ Сила клика: {power}\n👆 Кликов: {user['clicks']}"
                )
                screen = text, markup([[button(f"👆 ТАП (+{power})", "tap")], [button("🎉 Все события", "evt:home:0")]])
            else:
                raise EventError("Кнопка устарела. Откройте события заново.")
        except EventError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await cb.answer(note)
        try:
            await cb.message.edit_text(screen[0], reply_markup=screen[1])
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                await cb.message.answer(screen[0], reply_markup=screen[1])

    return router
