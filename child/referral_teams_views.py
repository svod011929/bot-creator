"""Private player team flows and reusable owner reports. No broadcasts or external calls."""

from __future__ import annotations

import html
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.referral_teams import CAPACITY, GOAL, GRACE, PERSONAL_GOAL, TZ, TeamError, week_window

BTN_TEAMS = "🤝 Команды"
RULES = (
    "Команда до 5 человек, состав закреплён до понедельника. Общая цель — 15 очков, "
    "минимум два участника и личный вклад от 3 очков для значка.\n"
    "Ежедневное участие: +1; новый верный ответ кампании: +2; новый подтверждённый друг: +3. "
    "Приглашения не обязательны: цель можно выполнить участием и заданиями.\n"
    "Командные очки не начисляются на денежный баланс или в клубные баллы. "
    "Полученный значок остаётся в коллекции."
)


def e(value, limit=1000):
    return html.escape(str(value)[:limit])


def b(title, data):
    return InlineKeyboardButton(text=title, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def dates(team):
    last = datetime.fromtimestamp(team["end_at"] - 1, TZ)
    return f"{datetime.fromtimestamp(team['start_at'], TZ):%d.%m.%Y} — {last:%d.%m.%Y} · МСК"


def number(raw):
    if not raw.isascii() or not raw.isdigit() or len(raw) > 19 or int(raw) > 2**63 - 1:
        raise TeamError("Кнопка устарела. Откройте команды заново.")
    return int(raw)


def private(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    return (
        bool(event.from_user)
        and isinstance(message, Message)
        and message.chat.type == "private"
        and message.chat.id == event.from_user.id
    )


def home(app, uid, page=0, *, ranking=False):
    data = app.teams.list_teams(uid, page, ranking=ranking)
    mine = app.teams.membership(uid)
    title = "🏆 <b>Рейтинг команд</b>" if ranking else "🤝 <b>Команды недели</b>"
    lines = [title, dates(data), f"Страница {data['page'] + 1} · команд: {data['total']}", "", RULES]
    if ranking:
        lines.append(
            "\nПорядок: командные очки, затем время создания и ID команды. Неделя обновляется в понедельник в 00:00 МСК."
        )
    rows = []
    for index, team in enumerate(data["rows"], data["page"] * 5 + 1):
        label = (
            "⏸ Вступление закрыто"
            if team["status"] == "paused"
            else "🟢 Открыта"
            if team["members"] < CAPACITY
            else "👥 Все места заняты"
        )
        lines += [
            "",
            f"<b>{index}. {e(team['name'], 48)}</b>",
            f"{team['members']}/5 участников · {team['score']} очков · {label}",
        ]
        rows.append([b(team["name"][:48], f"rt:view:{team['id']}")])
    if not data["rows"]:
        lines.append("\nПока команд нет. Можно создать свою и вместе с участниками пройти недельную цель.")
    action = "rank" if ranking else "list"
    nav = []
    if data["page"]:
        nav.append(b("⬅️", f"rt:{action}:{data['page'] - 1}"))
    if (data["page"] + 1) * 5 < data["total"]:
        nav.append(b("➡️", f"rt:{action}:{data['page'] + 1}"))
    rows.append(nav)
    if mine:
        rows.append([b("👥 Моя команда", f"rt:view:{mine['team_id']}")])
    else:
        rows.append([b("➕ Создать / продолжить", "rt:new")])
    rows += [
        [b("📆 Мои недели", "rt:history:0")],
        [b("🏆 Рейтинг", "rt:rank:0"), b("🏅 Мои значки", "rt:awards:0")],
        [b("🤝 Все команды", "rt:list:0"), b("🚀 Клуб", "club:home")],
    ]
    return "\n".join(lines), kb(rows)


def team_view(app, uid, tid, *, confirm=False):
    team = app.teams.team(uid, tid)
    now = app.teams._now()
    active = team["start_at"] <= now < team["end_at"]
    lines = [
        f"🤝 <b>{e(team['name'], 48)}</b>",
        dates(team),
        f"🎯 Общий прогресс: {team['score']}/{GOAL} очков",
        f"👥 Участников: {team['members']}/{CAPACITY}",
        "",
        RULES,
        "",
        "<b>Вклад участников</b>",
    ]
    for p in team["roster"]:
        lines.append(
            f"{e(p['full_name'] or 'Участник', 40)}: {p['score']} очков "
            f"(дней {p['visits']}, заданий {p['quizzes']}, друзей {p['friends']})"
        )
    rows = []
    if team["mine"]:
        mine = team["mine"]
        lines += ["", f"Твой вклад: {mine['score']}/{PERSONAL_GOAL} для значка."]
        if team["receipt"]:
            lines.append("🏅 Значок этой недели уже в коллекции.")
        elif now >= team["end_at"] + GRACE:
            lines.append("⌛ Срок получения значка закончился.")
        else:
            if team["score"] >= GOAL and team["members"] >= 2 and mine["score"] >= PERSONAL_GOAL:
                rows.append([b("🏅 Забрать значок команды", f"rt:claim:{tid}")])
            lines.append(f"Значок можно забрать до {datetime.fromtimestamp(team['end_at'] + GRACE, TZ):%d.%m.%Y %H:%M} МСК.")
        if active:
            if team["checked_today"]:
                lines.append("✅ Сегодняшний вклад +1 уже учтён.")
            else:
                rows.append([b("✅ Мой вклад сегодня · +1", f"rt:checkin:{tid}")])
        else:
            lines.append("🏁 Неделя закончилась: новые действия больше не учитываются.")
        lines.append("Задания и приглашения учитываются после вступления. Уже ожидавшие подтверждения друзья не засчитываются.")
    elif active and team["status"] == "open" and team["members"] < CAPACITY and not app.teams.membership(uid):
        if confirm:
            lines += ["", "Вступить? Выбор команды закрепится до конца этой недели."]
            rows.append([b("✅ Вступить до понедельника", f"rt:join:{tid}:{team['revision']}")])
        else:
            rows.append([b("🚀 Вступить в команду", f"rt:join_view:{tid}")])
    if team["status"] == "paused":
        lines.append("\n⏸ Новые вступления закрыты. Текущие участники продолжают вносить вклад до конца недели.")
    rows += [
        [b("🎯 Задания кампаний", "club:list:0"), b("🔄 Обновить", f"rt:view:{tid}")],
        [b("🤝 Все команды", "rt:list:0"), b("🏅 Мои значки", "rt:awards:0")],
    ]
    return "\n".join(lines), kb(rows)


def form_view(app, uid):
    form = app.teams.form_get(uid)
    if not form:
        raise TeamError("Сохранённой формы нет. Нажмите «Создать».")
    v = form["version"]
    _, start, end = week_window(app.teams._now())
    period = dates({"start_at": start, "end_at": end})
    if form["name"] is None:
        text = "📝 <b>Название команды</b>\nОтправьте название одной строкой, от 1 до 48 символов.\n\n" + RULES
        rows = []
    else:
        text = f"📝 <b>Создать «{e(form['name'], 48)}»?</b>\nТы займёшь первое место в команде текущей недели.\n\n{RULES}"
        rows = [[b("✅ Создать и вступить", f"rt:create:{v}")], [b("✏️ Изменить название", f"rt:edit:{v}")]]
    text += f"\n\nСрок команды: {period}.\n/cancel — удалить эту форму. Кнопка «Позже» сохраняет её."
    rows += [[b("⏳ Позже", f"rt:later:{v}"), b("🗑 Удалить форму", f"rt:discard:{v}")]]
    return text, kb(rows)


def awards(app, uid, page):
    data = app.teams.awards(uid, page)
    lines = ["🏅 <b>Командные значки</b>", f"Страница {data['page'] + 1} · значков: {data['total']}"]
    rows = []
    for row in data["rows"]:
        lines += [
            "",
            e(row["title"], 80),
            f"При получении: команда {row['team_score']} очков, личный вклад {row['personal_score']}.",
        ]
        rows.append([b("👥 Команда " + row["week"], f"rt:view:{row['team_id']}")])
    if not data["rows"]:
        lines.append("\nПервый значок появится после выполнения общей цели и твоего вклада от 3 очков.")
    nav = []
    if data["page"]:
        nav.append(b("⬅️", f"rt:awards:{data['page'] - 1}"))
    if (data["page"] + 1) * 8 < data["total"]:
        nav.append(b("➡️", f"rt:awards:{data['page'] + 1}"))
    return "\n".join(lines), kb(rows + [nav, [b("📆 Мои недели", "rt:history:0")], [b("🤝 Команды недели", "rt:list:0")]])


def history(app, uid, page):
    data = app.teams.history(uid, page)
    lines = [
        "📆 <b>Мои командные недели</b>",
        f"Страница {data['page'] + 1} · недель: {data['total']}",
        "Цели закрываются в понедельник. Получить заработанный значок можно ещё семь суток.",
    ]
    rows = []
    for team in data["rows"]:
        lines += [
            "",
            f"<b>{e(team['name'], 48)}</b> · {team['week']}",
            dates(team),
            "🏅 Значок получен" if team["claimed_at"] is not None else "Открой команду, чтобы проверить цели и срок получения.",
        ]
        rows.append([b(team["name"][:48], f"rt:view:{team['id']}")])
    if not data["rows"]:
        lines.append("\nТы ещё не выбирал команду. Начни с текущей недели.")
    nav = []
    if data["page"]:
        nav.append(b("⬅️", f"rt:history:{data['page'] - 1}"))
    if (data["page"] + 1) * 8 < data["total"]:
        nav.append(b("➡️", f"rt:history:{data['page'] + 1}"))
    return "\n".join(lines), kb(rows + [nav, [b("🤝 Команды недели", "rt:list:0")]])


def owner_list(app, uid, offset, page):
    data = app.teams.owner_list(uid, offset, page)
    lines = [
        "🤝 <b>Команды клуба · обзор владельца</b>",
        dates(data),
        f"Страница {data['page'] + 1} · команд: {data['total']}",
        "Недельная цель: 15 очков; значок при личном вкладе от 3.",
    ]
    rows = []
    for row in data["rows"]:
        lines += [
            "",
            f"<b>{e(row['name'], 48)}</b> · {row['score']} очков",
            f"Участников {row['members']}/5 · значков получено {row['claims']} · "
            + ("вступление открыто" if row["status"] == "open" else "вступление на паузе"),
        ]
        rows.append([b(row["name"][:48], f"rt:manage:{row['id']}")])
    if not data["rows"]:
        lines.append("\nВ эту неделю команды ещё не создавались.")
    nav = []
    if data["page"]:
        nav.append(b("⬅️", f"rt:admin:{offset}:{data['page'] - 1}"))
    if (data["page"] + 1) * 5 < data["total"]:
        nav.append(b("➡️", f"rt:admin:{offset}:{data['page'] + 1}"))
    rows.append(nav)
    weeks = []
    if offset < 52:
        weeks.append(b("⏪ Неделей раньше", f"rt:admin:{offset + 1}:0"))
    if offset:
        weeks.append(b("Неделей позже ⏩", f"rt:admin:{offset - 1}:0"))
    rows += [weeks, [b("📚 Контент бота", "rt:home")]]
    return "\n".join(lines), kb(rows)


def owner_team(app, uid, tid):
    team = app.teams.owner_team(uid, tid)
    lines = [
        f"🤝 <b>{e(team['name'], 48)}</b>",
        dates(team),
        f"Участников: {team['members']}/5 · очков: {team['score']}/15",
        "",
        "<b>Вклад участников</b>",
    ]
    for row in team["roster"]:
        lines.append(
            f"{e(row['full_name'] or 'Участник', 40)} · {row['score']} "
            f"(дней {row['visits']}, заданий {row['quizzes']}, друзей {row['friends']})"
        )
    rows = []
    if team["start_at"] <= app.teams._now() < team["end_at"]:
        action, title = ("pause", "⏸ Закрыть вступление") if team["status"] == "open" else ("activate", "▶️ Открыть вступление")
        rows.append([b(title, f"rt:{action}:{tid}:{team['revision']}")])
    lines += [
        "",
        "Пауза закрывает вступление. Цели, вклад и уже полученные значки сохраняются.",
        "Управление командой не отправляет сообщений участникам.",
    ]
    rows += [[b("🤝 Обзор команд", "rt:admin:0:0")], [b("📚 Контент бота", "rt:home")]]
    return "\n".join(lines), kb(rows)


class ActiveTeamForm(Filter):
    def __init__(self, app):
        self.app = app

    async def __call__(self, message):
        if not private(message) or message.text in {BTN_TEAMS, "🚀 Клуб"}:
            return False
        form = self.app.teams.form_get(message.from_user.id)
        return bool(form and form["active"])


def build_router(app):
    router = Router(name="referral-teams")

    @router.message(F.text == BTN_TEAMS)
    async def entry(message: Message, state: FSMContext):
        if not private(message):
            await message.answer("Откройте команды в личном чате с ботом.")
            return
        app.template.pause_forms(app, message.from_user.id)
        await state.clear()
        text, keyboard = home(app, message.from_user.id)
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("rt:"))
    async def action(cb: CallbackQuery, state: FSMContext):
        if not private(cb):
            await cb.answer("Откройте команды в личном чате с ботом.", show_alert=True)
            return
        uid, parts, note, writing = cb.from_user.id, cb.data.split(":"), "", False
        try:
            kind = parts[1] if len(parts) > 1 else ""
            if len(parts) == 3 and kind in {"list", "rank"}:
                screen = home(app, uid, number(parts[2]), ranking=kind == "rank")
            elif len(parts) == 3 and kind in {"view", "join_view"}:
                screen = team_view(app, uid, number(parts[2]), confirm=kind == "join_view")
            elif len(parts) == 4 and kind == "join":
                tid = number(parts[2])
                app.teams.join(uid, tid, number(parts[3]))
                note, screen = "🤝 Ты в команде!", team_view(app, uid, tid)
            elif len(parts) == 3 and kind in {"checkin", "claim"}:
                tid = number(parts[2])
                if kind == "checkin":
                    note = "✅ +1 командное очко" if app.teams.checkin(uid, tid) else "Сегодняшний вклад уже учтён"
                else:
                    app.teams.claim(uid, tid)
                    note = "🏅 Командный значок получен!"
                screen = team_view(app, uid, tid)
            elif cb.data == "rt:new":
                app.teams.form_begin(uid)
                screen, writing = form_view(app, uid), True
            elif len(parts) == 3 and kind == "edit":
                app.teams.form_edit(uid, number(parts[2]))
                screen, writing = form_view(app, uid), True
            elif len(parts) == 3 and kind == "create":
                tid = app.teams.create(uid, number(parts[2]))
                screen, note = team_view(app, uid, tid), "🤝 Команда создана!"
            elif len(parts) == 3 and kind in {"later", "discard"}:
                form = app.teams.form_get(uid)
                if not form or form["version"] != number(parts[2]):
                    raise TeamError("Форма изменилась. Откройте команды заново.")
                if kind == "discard":
                    app.teams.form_discard(uid, number(parts[2]))
                screen = home(app, uid)
            elif len(parts) == 3 and kind == "awards":
                screen = awards(app, uid, number(parts[2]))
            elif len(parts) == 3 and kind == "history":
                screen = history(app, uid, number(parts[2]))
            else:
                raise TeamError("Кнопка устарела. Откройте команды заново.")
        except TeamError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        if app.club.owner_check(uid):
            app.club.wizard_pause(uid)
        if not writing:
            app.teams.form_pause(uid)
        await state.clear()
        await cb.answer(note)
        try:
            await cb.message.edit_text(screen[0], reply_markup=screen[1])
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                await cb.message.answer(screen[0], reply_markup=screen[1])

    @router.message(F.text, ActiveTeamForm(app))
    async def name_input(message: Message):
        uid = message.from_user.id
        try:
            form = app.teams.form_get(uid)
            app.teams.form_name(uid, message.text, form["version"])
            screen = form_view(app, uid)
            await message.answer(screen[0], reply_markup=screen[1])
        except TeamError as exc:
            await message.answer(str(exc))

    return router
