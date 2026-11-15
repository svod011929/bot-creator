"""Private Telegram screens for the Star journey and its persistent content editor."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.star_catalog import CATEGORIES, CONSTELLATIONS, MISSIONS, SYMBOLS
from child.star_journey import EDITOR_FIELDS, JourneyError, period
from child.withdrawals import is_owner

BTN_JOURNEY = "🌌 Созвездия"
ADMIN_ACTIONS = {
    "admin",
    "manage",
    "new",
    "resume",
    "input",
    "save",
    "edit",
    "field",
    "publish_view",
    "publish",
    "pause",
    "activate",
    "copy",
    "later",
    "discard",
}
WRITING_ACTIONS = {"new", "resume", "input", "edit", "field"}
FIELD_LABELS = ("Тема", "Название", "Материал", "Вопрос", "Варианты", "Верный ответ", "Начало", "Срок")
PROMPTS = {
    "category": "Выберите тему карточки.",
    "title": "Название: до 80 символов.",
    "lesson": "Короткий полезный материал: до 700 символов. Ответ на вопрос должен следовать из него.",
    "question": "Вопрос по материалу: до 180 символов.",
    "answers": "Три разных варианта ответа, каждый с новой строки, до 80 символов на вариант.",
    "correct": "Выберите верный ответ кнопкой.",
    "starts": "Дата начала: ГГГГ-ММ-ДД или «сегодня». Даты и игровой день — по Москве (UTC+3).",
    "days": "Сколько календарных дней доступна карточка: от 1 до 365, включая день начала.",
}


def e(value, limit=1000):
    return html.escape(str(value)[:limit])


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back():
    return [button("🌌 Мой маршрут", "sky:home")]


def number(raw):
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
        raise JourneyError("Кнопка устарела. Откройте маршрут заново.")
    return int(raw)


def date_label(day):
    return f"{day[6:8]}.{day[4:6]}.{day[:4]}"


def card_status(card, today):
    if card["status"] == "draft":
        return "📝 Черновик"
    if card["status"] == "paused":
        return "⏸ Приостановлена"
    if card["end_day"] < today:
        return "⌛ Срок истёк"
    if card["start_day"] > today:
        return "📅 Запланирована"
    return "🟢 Доступна"


def home(app, uid):
    s = app.sky.overview(uid)
    p, t = s["profile"], s["today"]
    text = (
        f"🌌 <b>{e(app.s.get('sky_title', 'Мастерская созвездий'), 80)}</b>\n"
        f"{e(app.s.get('sky_intro', 'Открывай знания, тренируй внимание и собирай свою карту неба.'), 550)}\n\n"
        f"✨ Искры: <b>{p['sparks']}</b> · заработано всего: {p['earned']}\n"
        f"🔥 Серия: {s['streak']} дн. · рекорд: {p['best_streak']}\n"
        f"🌠 Созвездия: {len(s['built'])}/{len(CONSTELLATIONS)}\n\n"
        f"<b>Маршрут на {date_label(s['day'])}</b>\n"
        f"{'✅' if s['checked'] else '▫️'} Отметка дня · 10–22 искры по длине серии\n"
        f"{'✅' if t.get('cards', 0) >= 3 else '▫️'} Карточки: {t.get('cards', 0)}/3 · по 15 искр\n"
        f"{'✅' if t.get('memory', 0) else '▫️'} Память: {t.get('memory', 0)}/1 · 20 искр\n\n"
        "Начни с отметки и карточек. Первое созвездие можно зажечь в первой сессии, без друзей и платежей.\n"
        "Игровой день начинается в 00:00 МСК. За миссии награды забираются отдельно.\n\n"
        "<i>Искры — только игра: они не являются Telegram Stars, не выводятся и не входят в прежний баланс бота.</i>"
    )
    rows = []
    if not s["checked"]:
        rows.append([button("📅 Отметиться сегодня", f"sky:visit:{s['day']}")])
    rows += [
        [button("📚 Три карточки дня", "sky:deck"), button("🧠 Тренировка памяти", f"sky:memory:{s['day']}")],
        [button("🎯 Миссии", "sky:missions:day"), button("🌠 Карта созвездий", "sky:map")],
        [button("🏆 Рейтинг искр", "sky:rank:month"), button("🏅 Достижения", "sky:achievements")],
        [button("🧭 Тематические маршруты", "trk:list:0")],
        [button("🔄 Обновить маршрут", "sky:home")],
    ]
    if is_owner(app, uid):
        rows.append([button("⚙️ Карточки владельца", "sky:admin:0")])
    return text, markup(rows)


def deck_view(app, uid):
    result = app.sky.deck(uid)
    lines = [
        "📚 <b>Карточки дня</b>",
        f"{date_label(result['day'])} · до 3 карточек, по 15 искр за верный ответ.",
        "Состав закреплён до следующего дня. Пройденные карточки больше не повторяются.",
    ]
    rows = []
    for c in result["cards"]:
        done = c["answer"] and c["answer"]["answered_at"] is not None
        label = "✅ Пройдено" if done else "▫️ Доступно" if c["available"] else "⏸ Сейчас недоступно"
        lines += ["", f"<b>{e(c['title'], 80)}</b> · {e(CATEGORIES[c['category']])}", label]
        if c["available"]:
            rows.append([button(("✅ " if done else "📖 ") + c["title"][:55], f"sky:card:{c['id']}:{result['day']}")])
    if not result["cards"]:
        lines.append(
            "\nВсе доступные карточки пройдены или ещё не опубликованы. Проверь миссии и карту; новые карточки — в следующем дне."
        )
    return "\n".join(lines), markup(rows + [[button("🎯 Миссии дня", "sky:missions:day")], back()])


def card_view(app, uid, cid, day):
    c = app.sky.open_card(uid, cid, day)
    text = (
        f"{CATEGORIES[c['category']]} · <b>{e(c['title'], 80)}</b>\n\n{e(c['lesson'], 700)}\n\n"
        f"<b>{e(c['question'], 180)}</b>\n" + "\n".join(f"{i + 1}. {e(answer, 80)}" for i, answer in enumerate(c["answers"]))
    )
    answer = c["answer"]
    rows = []
    if answer["answered_at"] is not None:
        text += "\n\n✅ Пройдено. 15 искр уже начислены."
    else:
        if answer["retry_at"] > app.sky.now():
            text += f"\n\n⏳ Повтор через {answer['retry_at'] - app.sky.now()} сек."
        text += "\n\nПосле ошибки — пауза 30 сек., после трёх ошибок — 24 часа."
        rows = [
            [button(f"{i + 1}. {answer[:55]}", f"sky:answer:{cid}:{i}:{c['revision']}:{day}")]
            for i, answer in enumerate(c["answers"])
        ]
    return text, markup(rows + [[button("📚 Карточки дня", "sky:deck")], back()])


def memory_view(app, uid, day):
    m = app.sky.memory(uid, day)
    text = f"🧠 <b>Тренировка памяти</b> · {date_label(day)}\nПопытка {m['attempts']}/3. Награда — 20 искр один раз в день.\n\n"
    rows = []
    if m["phase"] == "preview":
        text += "Запомни порядок трёх символов:\n\n" + "   ".join(SYMBOLS[n] for n in m["sequence"])
        text += "\n\nНажми «Запомнил» — затем выбери эти символы по порядку. Это игровая разминка, без таймера."
        rows = [[button("✅ Запомнил", f"sky:ready:{day}:{m['version']}")]]
    elif m["phase"] == "answer":
        text += f"Выбери символ №{m['position'] + 1} из трёх."
        buttons = [button(s, f"sky:symbol:{day}:{m['version']}:{i}") for i, s in enumerate(SYMBOLS)]
        rows = [buttons[:3], buttons[3:]]
    elif m["phase"] == "done":
        text += "✅ Последовательность восстановлена, 20 искр начислены. Новая тренировка будет завтра."
    else:
        text += "Последовательность не совпала. "
        if m["attempts"] >= 3:
            text += "Три попытки на сегодня использованы. Возвращайся завтра."
        else:
            text += f"Повтор доступен через {max(0, m['retry_at'] - app.sky.now())} сек."
            rows = [[button("🔄 Повторить после паузы", f"sky:memory:{day}")]]
    return text, markup(rows + [[button("🎯 Миссии дня", "sky:missions:day")], back()])


def missions_view(app, uid, scope):
    result = app.sky.missions(uid, scope)
    names = {"story": "Первые открытия", "day": "Сегодня", "week": "Неделя"}
    metrics = {"visits": "дни", "cards": "карточки", "memory": "тренировки", "builds": "созвездия"}
    lines, rows = [f"🎯 <b>Миссии · {names[scope]}</b>", "Награды — игровые искры, забираются кнопкой."], []
    for m in result["rows"]:
        progress = " · ".join(f"{metrics[key]} {min(m['values'].get(key, 0), n)}/{n}" for key, n in m["needed"].items())
        lines += [
            "",
            f"{'✅' if m['claimed'] else '🎁' if m['ready'] else '▫️'} <b>{m['title']}</b> · {m['reward']} искр",
            progress,
        ]
        if m["ready"] and not m["claimed"]:
            rows.append([button("🎁 " + m["title"][:50], f"sky:claim:{m['key']}:{result['period']}")])
    rows += [
        [button("Старт", "sky:missions:story"), button("Сегодня", "sky:missions:day"), button("Неделя", "sky:missions:week")],
        back(),
    ]
    return "\n".join(lines), markup(rows)


def map_view(app, uid):
    s = app.sky.overview(uid)
    p, total = s["profile"], s["total"]
    lines, rows = (
        [
            "🌠 <b>Карта созвездий</b>",
            f"Доступно {p['sparks']} искр. При сборке они расходуются; заработанный рейтинг сохраняется.",
        ],
        [],
    )
    for key, item in CONSTELLATIONS.items():
        done = key in s["built"]
        lines += [
            "",
            f"{'✅' if done else '▫️'} <b>{item['title']}</b> · {item['cost']} искр",
            item["description"],
            f"Карточки: {min(total.get('cards', 0), item['cards'])}/{item['cards']} · "
            f"дни участия: {min(total.get('visits', 0), item['days'])}/{item['days']}",
        ]
        if not done:
            rows.append([button("🌟 Зажечь: " + item["title"], f"sky:build:{key}:{p['version']}")])
    return "\n".join(lines), markup(rows + [[button("🎯 Первые открытия", "sky:missions:story")], back()])


def ranking_view(app, scope):
    rows = app.sky.ranking(scope)
    lines = [
        "🏆 <b>Рейтинг заработанных искр</b>",
        "Этот календарный месяц (МСК)" if scope == "month" else "За всё время",
        "Сборка созвездий не уменьшает этот результат.",
    ]
    lines += [f"{i}. {e(r['full_name'] or 'Участник', 40)} — {r['score']} искр" for i, r in enumerate(rows, 1)]
    if not rows:
        lines.append("Пока нет результатов. Начни с отметки дня или карточки.")
    return "\n".join(lines), markup([[button("Месяц", "sky:rank:month"), button("Всё время", "sky:rank:all")], back()])


def achievements_view(app, uid):
    s = app.sky.overview(uid)
    lines = ["🏅 <b>Достижения</b>", *s["badges"]]
    if not s["badges"]:
        lines.append("Пройди первую карточку — появится первое достижение.")
    lines += ["", "📜 Последние изменения искр:"]
    kinds = {"visit": "Отметка дня", "card": "Карточка", "memory": "Память", "mission": "Миссия", "build": "Созвездие"}
    lines += [f"{kinds[r['kind']]} · {r['delta']:+d} искр" for r in s["history"]]
    return "\n".join(lines), markup([[button("🏅 Значки маршрутов", "trk:awards:0")], back()])


def admin_list(app, uid, page):
    result = app.sky.list_cards(uid, page)
    today = period("day", app.sky.now())
    lines, rows = (
        [
            "⚙️ <b>Карточки владельца</b>",
            f"Страница {result['page'] + 1} · всего {result['total']}",
            "Публикация добавляет карточку в общий каталог. Личные маршруты на сегодня уже закреплены.",
        ],
        [],
    )
    for c in result["rows"]:
        lines += ["", f"<b>{e(c['title'], 80)}</b> · {card_status(c, today)}"]
        rows.append([button(c["title"][:55], f"sky:manage:{c['id']}")])
    nav = []
    if result["page"]:
        nav.append(button("⬅️", f"sky:admin:{result['page'] - 1}"))
    if (result["page"] + 1) * 6 < result["total"]:
        nav.append(button("➡️", f"sky:admin:{result['page'] + 1}"))
    return "\n".join(lines), markup(
        rows + ([nav] if nav else []) + [[button("➕ Новая карточка", "sky:new"), button("📝 Продолжить", "sky:resume")], back()]
    )


def owner_card_view(app, uid, cid, confirm=False):
    c = app.sky.owner_card(uid, cid)
    text = (
        f"⚙️ <b>{e(c['title'], 80)}</b> · {CATEGORIES[c['category']]}\n"
        f"{card_status(c, period('day', app.sky.now()))}\n\n{e(c['lesson'], 700)}\n\n<b>{e(c['question'], 180)}</b>\n"
        + "\n".join(f"{'✅' if i == c['correct'] else '▫️'} {e(a, 80)}" for i, a in enumerate(c["answers"]))
        + (
            f"\n\nНачало: {date_label(c['start_day'])} · срок {c['days']} дн."
            + (f" · до {date_label(c['end_day'])}" if c["end_day"] else "")
            if c["days"]
            else "\n\nСтартовая карточка · без срока."
        )
        + "\nНаграда: 15 игровых искр один раз. Опубликованные условия закреплены; для новых — создайте копию."
    )
    rows = []
    if c["status"] == "draft":
        if confirm:
            text += "\n\nПодтвердите публикацию: карточка попадёт в каталог по указанному расписанию. Рассылки не будет."
            rows.append([button("✅ Подтвердить публикацию", f"sky:publish:{cid}:{c['revision']}")])
        else:
            rows += [
                [button("✏️ Изменить", f"sky:edit:{cid}:{c['revision']}")],
                [button("📣 Опубликовать", f"sky:publish_view:{cid}:{c['revision']}")],
            ]
    else:
        action, title = ("pause", "⏸ Приостановить") if c["status"] == "active" else ("activate", "▶️ Возобновить")
        rows.append([button(title, f"sky:{action}:{cid}:{c['revision']}")])
    return text, markup(
        rows + [[button("📋 Создать копию", f"sky:copy:{cid}")], [button("⚙️ Все карточки", "sky:admin:0")], back()]
    )


def editor_view(app, uid):
    draft = app.sky.editor_get(uid)
    if not draft:
        raise JourneyError("Сохранённого мастера нет. Нажмите «Новая карточка».")
    data, version = draft["data"], draft["version"]
    if draft["step"] == "confirm":
        text = (
            f"📝 <b>Предпросмотр карточки</b>\n{CATEGORIES[data['category']]} · {e(data['title'], 80)}\n\n"
            f"{e(data['lesson'], 700)}\n\n<b>{e(data['question'], 180)}</b>\n"
            + "\n".join(f"{'✅' if i == data['correct'] else '▫️'} {e(a, 80)}" for i, a in enumerate(data["answers"]))
            + f"\n\nНачало {date_label(data['starts'])} МСК, {data['days']} дн. Награда: 15 искр."
            + "\nСохранение создаёт черновик; публикация подтверждается отдельно."
        )
        fields = [
            button("✏️ " + label, f"sky:field:{field}:{version}") for field, label in zip(EDITOR_FIELDS, FIELD_LABELS, strict=True)
        ]
        rows = [[button("💾 Сохранить черновик", f"sky:save:{version}")]] + [fields[i : i + 2] for i in range(0, len(fields), 2)]
    else:
        field = draft["step"]
        text = (
            f"📝 <b>Карточка · шаг {EDITOR_FIELDS.index(field) + 1}/8</b>\n{PROMPTS[field]}"
            "\n\n/cancel — удалить незавершённую форму."
        )
        rows = []
        if field == "category":
            rows = [[button(title, f"sky:input:category:{key}:{version}")] for key, title in CATEGORIES.items()]
        elif field == "correct":
            text += "\n\n" + "\n".join(f"{i + 1}. {e(a, 80)}" for i, a in enumerate(data["answers"]))
            rows = [
                [button(f"{i + 1}. {answer[:55]}", f"sky:input:correct:{i}:{version}")]
                for i, answer in enumerate(data["answers"])
            ]
    return text, markup(rows + [[button("⏸ Отложить", "sky:later"), button("🗑 Удалить форму", "sky:discard")]])


async def open_journey(message, state, app):
    if message.chat.type != "private":
        await message.answer("Откройте созвездия в личном чате с ботом.")
        return
    if is_owner(app, message.from_user.id):
        app.sky.editor_pause(message.from_user.id)
    await state.clear()
    try:
        text, kb = home(app, message.from_user.id)
    except JourneyError as exc:
        await message.answer(str(exc))
        return
    await message.answer(text, reply_markup=kb)


class ActiveEditor(Filter):
    def __init__(self, app):
        self.app = app

    async def __call__(self, message):
        if message.chat.type != "private" or not message.from_user or not is_owner(self.app, message.from_user.id):
            return False
        draft = self.app.sky.editor_get(message.from_user.id)
        return bool(draft and draft["active"])


def build_router(app):
    r = Router(name="star-journey")

    @r.message(F.text == BTN_JOURNEY)
    async def open_menu(message: Message, state: FSMContext):
        await open_journey(message, state, app)

    @r.callback_query(F.data.startswith("sky:"))
    async def action(cb: CallbackQuery, state: FSMContext):
        if not isinstance(cb.message, Message) or cb.message.chat.type != "private" or cb.message.chat.id != cb.from_user.id:
            await cb.answer("Откройте созвездия в своём личном чате с ботом.", show_alert=True)
            return
        uid, parts, note = cb.from_user.id, cb.data.split(":"), ""
        kind = parts[1] if len(parts) > 1 else ""
        try:
            if kind in ADMIN_ACTIONS and not is_owner(app, uid):
                raise JourneyError("Доступно только текущему владельцу бота.")
            if cb.data == "sky:home":
                text, kb = home(app, uid)
            elif kind == "visit" and len(parts) == 3:
                amount = app.sky.checkin(uid, parts[2])
                note = f"📅 +{amount} искр за отметку дня"
                text, kb = home(app, uid)
            elif cb.data == "sky:deck":
                text, kb = deck_view(app, uid)
            elif kind == "card" and len(parts) == 4:
                text, kb = card_view(app, uid, number(parts[2]), parts[3])
            elif kind == "answer" and len(parts) == 6:
                result = app.sky.answer(uid, number(parts[2]), number(parts[3]), number(parts[4]), parts[5])
                note = "✅ +15 игровых искр" if result["correct"] else "Ответ не совпал. Прочитай материал и повтори после паузы."
                text, kb = card_view(app, uid, number(parts[2]), parts[5])
            elif kind == "memory" and len(parts) == 3:
                text, kb = memory_view(app, uid, parts[2])
            elif kind in {"ready", "symbol"} and len(parts) == (4 if kind == "ready" else 5):
                amount = app.sky.memory_step(uid, parts[2], number(parts[3]), number(parts[4]) if kind == "symbol" else None)
                note = (
                    "✅ +20 игровых искр"
                    if amount > 0
                    else "Порядок не совпал. Можно повторить после паузы."
                    if amount < 0
                    else ""
                )
                text, kb = memory_view(app, uid, parts[2])
            elif kind == "missions" and len(parts) == 3:
                text, kb = missions_view(app, uid, parts[2])
            elif kind == "claim" and len(parts) == 4:
                amount = app.sky.claim(uid, parts[2], parts[3])
                note = f"🎁 +{amount} игровых искр"
                text, kb = missions_view(app, uid, MISSIONS[parts[2]][0])
            elif cb.data == "sky:map":
                text, kb = map_view(app, uid)
            elif kind == "build" and len(parts) == 4:
                app.sky.build(uid, parts[2], number(parts[3]))
                note = "🌠 Созвездие зажжено!"
                text, kb = map_view(app, uid)
            elif kind == "rank" and len(parts) == 3:
                text, kb = ranking_view(app, parts[2])
            elif cb.data == "sky:achievements":
                text, kb = achievements_view(app, uid)
            elif kind == "admin" and len(parts) == 3:
                text, kb = admin_list(app, uid, number(parts[2]))
            elif kind == "manage" and len(parts) == 3:
                text, kb = owner_card_view(app, uid, number(parts[2]))
            elif kind in {"new", "resume"} and len(parts) == 2:
                if kind == "resume" and not app.sky.editor_get(uid):
                    raise JourneyError("Сохранённой формы нет. Нажмите «Новая карточка».")
                app.sky.editor_begin(uid)
                text, kb = editor_view(app, uid)
            elif kind == "edit" and len(parts) == 4:
                app.sky.editor_begin(uid, number(parts[2]), number(parts[3]))
                text, kb = editor_view(app, uid)
            elif kind == "field" and len(parts) == 4:
                app.sky.editor_field(uid, parts[2], number(parts[3]))
                text, kb = editor_view(app, uid)
            elif kind == "input" and len(parts) == 5:
                draft = app.sky.editor_get(uid)
                if not draft or parts[2] not in {"correct", "category"} or draft["step"] != parts[2]:
                    raise JourneyError("Откройте актуальный шаг мастера.")
                value = number(parts[3]) if parts[2] == "correct" else parts[3]
                app.sky.editor_input(uid, value, number(parts[4]))
                text, kb = editor_view(app, uid)
            elif kind == "save" and len(parts) == 3:
                cid = app.sky.editor_save(uid, number(parts[2]))
                text, kb = owner_card_view(app, uid, cid)
                note = "Черновик сохранён"
            elif kind == "publish_view" and len(parts) == 4:
                c = app.sky.owner_card(uid, number(parts[2]))
                if c["status"] != "draft" or c["revision"] != number(parts[3]):
                    raise JourneyError("Карточка изменилась. Откройте её заново.")
                text, kb = owner_card_view(app, uid, c["id"], confirm=True)
            elif kind in {"publish", "pause", "activate"} and len(parts) == 4:
                cid, revision = number(parts[2]), number(parts[3])
                if kind == "publish":
                    app.sky.publish(uid, cid, revision)
                else:
                    app.sky.set_status(uid, cid, "paused" if kind == "pause" else "active", revision)
                text, kb = owner_card_view(app, uid, cid)
            elif kind == "copy" and len(parts) == 3:
                text, kb = owner_card_view(app, uid, app.sky.copy_card(uid, number(parts[2])))
            elif cb.data in {"sky:later", "sky:discard"}:
                if kind == "discard":
                    app.sky.editor_discard(uid)
                else:
                    app.sky.editor_pause(uid)
                text, kb = admin_list(app, uid, 0)
            else:
                raise JourneyError("Кнопка устарела. Откройте созвездия заново.")
        except JourneyError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        if kind not in WRITING_ACTIONS and is_owner(app, uid):
            app.sky.editor_pause(uid)
        await state.clear()
        await cb.answer(note)
        try:
            await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)

    @r.message(F.text, ActiveEditor(app))
    async def editor_input(message: Message):
        uid = message.from_user.id
        try:
            draft = app.sky.editor_get(uid)
            if draft["step"] in {"category", "correct", "confirm"}:
                await message.answer("Выберите действие кнопкой ниже.")
            else:
                app.sky.editor_input(uid, message.text)
            text, kb = editor_view(app, uid)
            await message.answer(text, reply_markup=kb)
        except JourneyError as exc:
            await message.answer(str(exc))

    return r
