"""Ordered lesson screens and Creator-reused learning track editors."""

from __future__ import annotations

import html
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.star_journey import TZ, day_date, period
from child.star_tracks import FIELDS, TrackError

BTN_TRACKS = "🧭 Маршруты"
LABELS = ("Название", "Описание", "Этапы", "Начало", "Дней", "Значок")
PROMPTS = {
    "title": "Название маршрута: до 80 символов.",
    "description": "Описание и цель: до 900 символов.",
    "starts": "Начало: ГГГГ-ММ-ДД или «сегодня», не позже чем через год. Даты — по Москве.",
    "days": "Срок: от 1 до 365 календарных дней, включая первый и последний день.",
    "badge": "Название итогового значка: до 60 символов, можно с эмодзи.",
}


def e(value, limit=1000):
    return html.escape(str(value)[:limit])


def b(title, data):
    return InlineKeyboardButton(text=title, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def phase(c, now):
    if c["status"] == "draft":
        return "📝 Черновик"
    day = period("day", now)
    if day > c["end_day"]:
        return "🏁 Завершён"
    if c["status"] == "paused":
        return "⏸ Новые старты на паузе"
    return "📅 Скоро" if day < c["start_day"] else "🟢 Доступен"


def dates(c):
    last = c["end_day"] or (day_date(c["start_day"]) + timedelta(days=c["days"] - 1)).strftime("%Y%m%d")
    return f"{day_date(c['start_day']):%d.%m.%Y} — {day_date(last):%d.%m.%Y} · МСК"


def overview_text(c):
    return (
        f"🧭 <b>{e(c['title'], 80)}</b>\n{e(c['description'], 900)}\n\n{dates(c)}\n"
        f"📚 Этапов: {len(c['cards'])} · итоговый значок: {e(c['badge'], 60)}\n"
        "Этапы открываются по порядку. После ошибки — пауза 30 секунд, после третьей — 24 часа. "
        "Пройденные материалы можно перечитать.\n"
        "Значок за маршрут не начисляет деньги или игровые искры. Карточки дня проходят отдельно."
    )


def admin_list(app, uid, page):
    data = app.tracks.list_tracks(uid, page, owner=True)
    lines = ["🧭 <b>Тематические маршруты</b>", f"Страница {data['page'] + 1} · всего {data['total']}"]
    rows = []
    for c in data["rows"]:
        lines += ["", f"<b>{e(c['title'], 80)}</b> · {phase(c, app.tracks._now())}", dates(c)]
        rows.append([b(c["title"][:55], f"trk:manage:{c['id']}")])
    nav = []
    if data["page"]:
        nav.append(b("⬅️", f"trk:admin:{data['page'] - 1}"))
    if (data["page"] + 1) * 5 < data["total"]:
        nav.append(b("➡️", f"trk:admin:{data['page'] + 1}"))
    return "\n".join(lines), kb(
        rows + [nav, [b("➕ Создать", "trk:new"), b("📝 Продолжить", "trk:resume")], [b("📚 Контент бота", "trk:home")]]
    )


def owner_track_view(app, uid, tid, confirm=False):
    c = app.tracks.owner_track(uid, tid)
    text = overview_text(c) + f"\n\n{phase(c, app.tracks._now())} · версия {c['revision']}\n"
    text += f"Начали: {c['participants']} · завершили: {c['completed']} · значок получили: {c['claimed']}\n\n<b>Этапы</b>"
    for row in c["stages"]:
        text += f"\n{row['position'] + 1}. ID {row['card_id']} · {e(row['title'], 24)} · прошли {row['passed']}"
    rows = []
    if c["status"] == "draft":
        if confirm:
            text += "\n\nОпубликовать? Тексты, вопросы и порядок закрепятся. "
            text += "Последующая пауза исходной карточки не меняет материал в этом маршруте."
            rows.append([b("✅ Опубликовать", f"trk:publish:{tid}:{c['revision']}")])
        else:
            rows += [
                [b("✏️ Изменить", f"trk:edit:{tid}:{c['revision']}")],
                [b("📢 Публикация", f"trk:publish_view:{tid}:{c['revision']}")],
            ]
    elif c["end_day"] >= period("day", app.tracks._now()):
        action, label = ("pause", "⏸ Закрыть новые старты") if c["status"] == "active" else ("activate", "▶️ Открыть старты")
        rows.append([b(label, f"trk:{action}:{tid}:{c['revision']}")])
    rows += [[b("📄 Создать копию", f"trk:copy:{tid}")], [b("⬅️ Каталог", "trk:admin:0")], [b("📚 Контент бота", "trk:home")]]
    return text, kb(rows)


def source_view(app, uid, page=0):
    form = app.tracks.editor_get(uid)
    if not form or form["step"] != "cards":
        raise TrackError("Откройте шаг выбора этапов.")
    version, chosen = form["version"], form["data"].get("cards", [])
    selected = app.tracks.selected(uid, chosen)
    data = app.tracks.sources(uid, page)
    lines = [
        "📚 <b>Этапы маршрута · выбрано " + str(len(chosen)) + "/12</b>",
        "Нажимайте карточки в порядке прохождения. Для удаления нажмите повторно. "
        "Нужно от 3 до 12 карточек; можно отправить ID через пробел или запятую.",
        "",
    ]
    rows = []
    for pos, row in enumerate(selected, 1):
        lines.append(f"{pos}. ID {row['id']} · {e(row['title'], 32)}")
        rows.append([b(f"➖ {pos}. " + row["title"][:35], f"trk:pick:{row['id']}:{version}:{data['page']}")])
    lines += [
        "",
        f"Каталог опубликованных карточек · страница {data['page'] + 1}",
        "При публикации их сроки должны покрывать весь маршрут.",
    ]
    for row in data["rows"]:
        prefix = "✅" if row["id"] in chosen else "➕"
        availability = (
            "без срока"
            if row["start_day"] == "00010101" and row["end_day"] == "99991231"
            else f"{day_date(row['start_day']):%d.%m.%Y} — {day_date(row['end_day']):%d.%m.%Y}"
        )
        lines.append(f"ID {row['id']} · {e(row['title'], 32)} · {availability}")
        rows.append([b(f"{prefix} ID {row['id']} · " + row["title"][:35], f"trk:pick:{row['id']}:{version}:{data['page']}")])
    nav = []
    if data["page"]:
        nav.append(b("⬅️", f"trk:sources:{data['page'] - 1}:{version}"))
    if (data["page"] + 1) * 6 < data["total"]:
        nav.append(b("➡️", f"trk:sources:{data['page'] + 1}:{version}"))
    rows += [nav, [b("✅ Далее", f"trk:picked:{version}")], [b("⏳ Позже", "trk:later"), b("🗑 Удалить форму", "trk:discard")]]
    return "\n".join(lines), kb(rows)


def editor_view(app, uid):
    form = app.tracks.editor_get(uid)
    if not form:
        raise TrackError("Сохранённой формы нет. Нажмите «Создать».")
    step, data, v = form["step"], form["data"], form["version"]
    if step == "cards":
        return source_view(app, uid)
    if step == "confirm":
        lines = ["📝 <b>Предпросмотр маршрута</b>"]
        for field, label in zip(FIELDS, LABELS, strict=True):
            value = ", ".join(map(str, data[field])) if field == "cards" else data[field]
            lines.append(f"<b>{label}:</b> {e(value, 900)}")
        lines.append("\nСохранение создаёт черновик. Публикация подтверждается отдельно.")
        rows = [[b("💾 Сохранить черновик", f"trk:save:{v}")]]
        fields = [b(label, f"trk:field:{field}:{v}") for field, label in zip(FIELDS, LABELS, strict=True)]
        rows += [fields[i : i + 2] for i in range(0, len(fields), 2)]
        text = "\n".join(lines)
    else:
        text = f"📝 <b>Маршрут · шаг {FIELDS.index(step) + 1}/6</b>\n{PROMPTS[step]}\n\nОтправьте значение сообщением."
        rows = []
    return text, kb(rows + [[b("⏳ Позже", "trk:later"), b("🗑 Удалить форму", "trk:discard")]])


def lesson_text(c):
    return f"📖 <b>{e(c['title'], 80)}</b>\n\n{e(c['lesson'], 700)}\n\n<b>{e(c['question'], 180)}</b>\n" + "\n".join(
        f"{i + 1}. {e(a, 80)}" for i, a in enumerate(c["answers"])
    )


def preview(app, uid, tid, position=None):
    c = app.tracks.owner_track(uid, tid)
    if position is None:
        text, rows = overview_text(c), []
        for row in c["stages"]:
            rows.append([b(f"👁 {row['position'] + 1}. " + row["title"][:40], f"trk:preview_step:{tid}:{row['position']}")])
    else:
        if not 0 <= position < len(c["stages"]):
            raise TrackError("Этап не найден.")
        text, rows = lesson_text(c["stages"][position]), [[b("⬅️ Предпросмотр маршрута", f"trk:preview:{tid}")]]
    text = "👁 <b>Предпросмотр для участника</b>\n\n" + text
    text += "\n\n<i>Только предпросмотр: прохождение не записывается, ответы и значки не засчитываются.</i>"
    rows += [[b("⚙️ К маршруту", f"trk:manage:{tid}")], [b("📚 Контент бота", "trk:home")]]
    return text, kb(rows)


def player_list(app, uid, page=0, *, mine=False):
    data = app.tracks.list_tracks(uid, page, mine=mine)
    lines = [
        "🧭 <b>Мои маршруты</b>" if mine else "🧭 <b>Тематические маршруты</b>",
        f"Страница {data['page'] + 1} · маршрутов: {data['total']}",
        "Выбирай тему, проходи этапы по порядку и собирай значки.",
    ]
    rows = []
    for c in data["rows"]:
        lines += ["", f"<b>{e(c['title'], 80)}</b> · {phase(c, app.tracks._now())}", dates(c)]
        rows.append([b(c["title"][:55], f"trk:view:{c['id']}")])
    if not data["rows"]:
        lines.append("\nЗдесь пока пусто. Новые маршруты публикует владелец; сейчас доступны карточки дня и тренировка памяти.")
    action = "mine" if mine else "list"
    nav = []
    if data["page"]:
        nav.append(b("⬅️", f"trk:{action}:{data['page'] - 1}"))
    if (data["page"] + 1) * 5 < data["total"]:
        nav.append(b("➡️", f"trk:{action}:{data['page'] + 1}"))
    rows += [
        nav,
        [b("📂 Мои маршруты", "trk:mine:0"), b("🏅 Мои значки", "trk:awards:0")],
        [b("🧭 Все маршруты", "trk:list:0"), b("🌌 Созвездия", "sky:home")],
    ]
    return "\n".join(lines), kb(rows)


def player_view(app, uid, tid):
    c = app.tracks.player_track(uid, tid)
    p, now, rows = c["player"], app.tracks._now(), []
    lines = [overview_text(c), "", phase(c, now)]
    if p:
        lines += ["", f"<b>Прогресс: {p['current_step']}/{len(c['cards'])}</b>"]
        for stage in c["stages"]:
            pos = stage["position"]
            label = "✅" if pos < p["current_step"] else "▶️" if pos == p["current_step"] else "🔒"
            lines.append(f"{label} {pos + 1}. {e(stage['title'], 40)}")
            if pos < p["current_step"]:
                rows.append([b(f"📖 Повторить {pos + 1}", f"trk:stage:{tid}:{pos}")])
        if p["badge_claimed_at"] is not None:
            lines.append("\n🏅 Значок уже в коллекции: " + e(p["badge_title"], 60))
        elif p["completed_at"] is not None and now < c["claim_until"]:
            rows.insert(0, [b("🏅 Забрать значок", f"trk:claim:{tid}")])
        elif p["completed_at"] is not None:
            lines.append("\n⌛ Срок получения значка закончился.")
        elif now < c["ends_at"]:
            rows.insert(0, [b(f"📖 Продолжить · этап {p['current_step'] + 1}", f"trk:stage:{tid}:{p['current_step']}")])
        else:
            lines.append("\n🏁 Срок прохождения закончился. Пройденные материалы остались доступны для повторения.")
        lines.append(f"Значок можно забрать до {datetime.fromtimestamp(c['claim_until'], TZ):%d.%m.%Y %H:%M} МСК.")
    else:
        lines += ["", "Этапы:"] + [f"{s['position'] + 1}. {e(s['title'], 40)}" for s in c["stages"]]
        if c["status"] == "active" and c["start_day"] <= period("day", now) <= c["end_day"]:
            rows.append([b("🚀 Начать маршрут", f"trk:join:{tid}:{c['revision']}")])
    rows += [[b("🔄 Обновить", f"trk:view:{tid}"), b("📂 Мои маршруты", "trk:mine:0")], [b("🧭 Все маршруты", "trk:list:0")]]
    return "\n".join(lines), kb(rows)


def stage_view(app, uid, tid, position):
    c = app.tracks.open_stage(uid, tid, position)
    text, rows = f"Этап {position + 1}\n" + lesson_text(c), []
    if c["answer"]["answered_at"] is not None:
        text += "\n\n✅ Этот этап пройден. Повторение не меняет результат."
    else:
        retry = c["answer"]["retry_at"] - app.tracks._now()
        text += f"\n\n⏳ Повтор через {retry} сек." if retry > 0 else "\n\nВыбери ответ, чтобы открыть следующий этап."
        rows = [[b(f"{i + 1}. {a[:50]}", f"trk:answer:{tid}:{position}:{i}:{c['version']}")] for i, a in enumerate(c["answers"])]
    return text, kb(rows + [[b("⬅️ К маршруту", f"trk:view:{tid}")]])


def awards(app, uid, page):
    data = app.tracks.awards(uid, page)
    lines = ["🏅 <b>Значки маршрутов</b>", f"Страница {data['page'] + 1} · значков: {data['total']}"]
    rows = []
    for row in data["rows"]:
        lines += ["", f"<b>{e(row['badge_title'], 60)}</b>", e(row["title"], 80)]
        rows.append([b("📖 Повторить материал", f"trk:view:{row['track_id']}")])
    if not data["rows"]:
        lines.append("\nПройди тематический маршрут и забери свой первый значок.")
    nav = []
    if data["page"]:
        nav.append(b("⬅️", f"trk:awards:{data['page'] - 1}"))
    if (data["page"] + 1) * 8 < data["total"]:
        nav.append(b("➡️", f"trk:awards:{data['page'] + 1}"))
    return "\n".join(lines), kb(rows + [nav, [b("📂 Мои маршруты", "trk:mine:0")], [b("🧭 Все маршруты", "trk:list:0")]])


def number(raw):
    if not raw.isascii() or not raw.isdigit() or len(raw) > 19 or int(raw) > 2**63 - 1:
        raise TrackError("Кнопка устарела. Откройте маршруты заново.")
    return int(raw)


def private(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    return (
        bool(event.from_user)
        and isinstance(message, Message)
        and message.chat.type == "private"
        and message.chat.id == event.from_user.id
    )


async def open_tracks(message, state, app):
    if not private(message):
        await message.answer("Откройте маршруты в личном чате с ботом.")
        return
    app.template.pause_forms(app, message.from_user.id)
    await state.clear()
    text, keyboard = player_list(app, message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


def build_router(app):
    router = Router(name="star-learning-tracks")

    @router.message(F.text == BTN_TRACKS)
    async def entry(message: Message, state: FSMContext):
        await open_tracks(message, state, app)

    @router.callback_query(F.data.startswith("trk:"))
    async def action(cb: CallbackQuery, state: FSMContext):
        if not private(cb):
            await cb.answer("Откройте маршруты в личном чате с ботом.", show_alert=True)
            return
        uid, parts, note = cb.from_user.id, cb.data.split(":"), ""
        try:
            kind = parts[1] if len(parts) > 1 else ""
            if len(parts) == 3 and kind in {"list", "mine"}:
                screen = player_list(app, uid, number(parts[2]), mine=kind == "mine")
            elif len(parts) == 3 and kind == "view":
                screen = player_view(app, uid, number(parts[2]))
            elif len(parts) == 4 and kind == "join":
                tid = number(parts[2])
                app.tracks.join(uid, tid, number(parts[3]))
                note, screen = "🚀 Маршрут начат!", player_view(app, uid, tid)
            elif len(parts) == 4 and kind == "stage":
                screen = stage_view(app, uid, number(parts[2]), number(parts[3]))
            elif len(parts) == 6 and kind == "answer":
                tid, pos, choice, v = map(number, parts[2:])
                correct = app.tracks.answer(uid, tid, pos, choice, v)
                note = "✅ Этап пройден!" if correct else "Пока неверно. Прочитай материал и попробуй после паузы."
                screen = player_view(app, uid, tid) if correct else stage_view(app, uid, tid, pos)
            elif len(parts) == 3 and kind == "claim":
                tid = number(parts[2])
                app.tracks.claim(uid, tid)
                note, screen = "🏅 Значок получен!", player_view(app, uid, tid)
            elif len(parts) == 3 and kind == "awards":
                screen = awards(app, uid, number(parts[2]))
            else:
                raise TrackError("Кнопка устарела. Откройте маршруты заново.")
        except TrackError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        app.template.pause_forms(app, uid)
        await state.clear()
        await cb.answer(note)
        try:
            await cb.message.edit_text(screen[0], reply_markup=screen[1])
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                await cb.message.answer(screen[0], reply_markup=screen[1])

    return router
