"""Private Telegram screens and a persistent campaign wizard for the referral club."""

from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.referral_club import INTERESTS, ONBOARD_OPTIONS, WIZARD_FIELDS, ClubError
from child.withdrawals import is_owner

BTN_CLUB = "🚀 Клуб"
TZ = ZoneInfo("Europe/Moscow")
ADMIN_ACTIONS = {
    "admin",
    "manage",
    "new",
    "resume",
    "correct",
    "save",
    "publish_view",
    "publish",
    "pause",
    "activate",
    "copy",
    "edit",
    "field",
    "later",
    "discard",
}
WRITING_ACTIONS = {"new", "resume", "correct", "edit", "field"}
FIELD_LABELS = ("Название", "Описание", "Цель", "Баллы за друга", "Лимит", "Срок", "Вопрос", "Варианты ответа", "Верный ответ")
PROMPTS = {
    "title": "Название кампании: от 1 до 80 символов.",
    "description": "Описание и польза для участника: до 900 символов.",
    "goal": "Личная цель участника: от 1 до 1000 подтверждённых приглашений.",
    "reward": "Клубные баллы за подтверждённого друга: от 1 до 1000. Это не денежная выплата.",
    "quota": "Общий лимит подтверждений: до 100000, не меньше личной цели. Места назначаются при зачёте.",
    "days": "Срок: от 1 до 365 дней с момента публикации.",
    "question": "Вопрос задания для участника: до 180 символов. Ответ должен следовать из описания кампании.",
    "answers": "Пришлите три разных варианта ответа, каждый с новой строки, до 80 символов на вариант.",
    "correct": "Выберите правильный ответ кнопкой ниже.",
}


def e(value, limit=1000):
    return html.escape(str(value)[:limit])


def b(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back():
    return [b("🚀 Клуб", "club:home")]


def number(raw):
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
        raise ClubError("Кнопка устарела. Откройте клуб заново.")
    return int(raw)


def status(c, at):
    if c["status"] == "draft":
        return "📝 Черновик"
    if c["ends_at"] and at >= c["ends_at"]:
        return "⌛ Срок истёк"
    return "🟢 Активна" if c["status"] == "active" else "⏸ Новые входы приостановлены"


def end_label(c):
    return f"{datetime.fromtimestamp(c['ends_at'], TZ):%d.%m.%Y %H:%M} МСК" if c["ends_at"] else "без срока"


def home(app, uid):
    s = app.club.summary(uid)
    p = s["member"]
    level = ("Новичок", "Помощник", "Амбассадор", "Проводник", "Лидер")[sum(s["qualified"] >= n for n in (1, 3, 10, 25))]
    lines = [
        f"🚀 <b>{e(app.s.get('club_title', 'Клуб амбассадоров'), 80)}</b>",
        e(app.s.get("club_intro", "Выбирай кампанию, знакомься с её задачей и приглашай заинтересованных друзей."), 600),
        "",
        f"🏅 Статус: <b>{level}</b>",
        f"✨ Клубные баллы: <b>{p['points']}</b>",
        f"👥 Подтверждено: {s['qualified']} · ожидают шагов: {s['pending']}",
    ]
    rows = []
    if not p["onboarding_complete"]:
        lines.extend(("", "Первый шаг — знакомство: выбери интерес и ответь на короткий вопрос. Награда: 20 клубных баллов."))
        rows.append([b("👋 Начать знакомство", "club:onboard")])
    else:
        lines.extend(
            (
                "",
                f"Твой интерес: {INTERESTS[p['interest']]}",
                "Задание каждой кампании даёт 10 баллов один раз. Приглашать друзей для этого не требуется.",
            )
        )
    invitation = s["invitation"]
    if invitation and invitation["status"] == "joined" and (not invitation["ends_at"] or app.club._now() < invitation["ends_at"]):
        lines.extend(("", f"🎯 Тебя пригласили в «{e(invitation['title'], 80)}». Для зачёта пройди знакомство и задание."))
        rows.append([b("🎯 Кампания приглашения", f"club:campaign:{invitation['campaign_id']}")])
    rows += [
        [b("📣 Кампании", "club:list:0"), b("👥 Мои приглашения", "club:invites:0")],
        [b("🏆 Рейтинг клуба", "club:rank"), b("🏅 Мои достижения", "club:achievements")],
        [b("🤝 Команды недели", "rt:list:0")],
        [b("🔄 Проверить условия доступа", "club:check")],
    ]
    if is_owner(app, uid):
        rows.append([b("⚙️ Управление кампаниями", "club:admin:0")])
    lines.extend(("", "<i>Клубные баллы — прогресс участия. Денежный баланс и ручные выплаты бота учитываются отдельно.</i>"))
    return "\n".join(lines), kb(rows)


def onboarding(app, uid):
    p = app.club.summary(uid)["member"]
    if p["onboarding_complete"]:
        return home(app, uid)
    if not p["interest"]:
        return (
            "👋 <b>Знакомство с клубом</b>\nЧто тебе интереснее? Выбор сохраняется в профиле.",
            kb([[b(title, f"club:interest:{key}:{p['version']}")] for key, title in INTERESTS.items()] + [back()]),
        )
    text = "👋 <b>Один вопрос перед стартом</b>\nЧто означают клубные баллы?\nВыбери ответ. За знакомство — 20 баллов."
    return text, kb([[b(text, f"club:onanswer:{i}:{p['version']}")] for i, text in enumerate(ONBOARD_OPTIONS)] + [back()])


def campaign_list(app, uid, page=0, owner=False):
    result = app.club.list_campaigns(uid, page=page, owner=owner)
    lines = [
        "⚙️ <b>Кампании владельца</b>" if owner else "📣 <b>Кампании клуба</b>",
        f"Страница {result['page'] + 1} · всего {result['total']}",
    ]
    rows = []
    for c in result["rows"]:
        lines.extend(
            (
                "",
                f"<b>{e(c['title'], 80)}</b> · {status(c, app.club._now())}",
                f"Цель: {c['goal']} друзей · +{c['reward']} баллов за зачёт · мест {max(0, c['quota'] - c['qualified_count'])}",
            )
        )
        rows.append([b(c["title"][:60], f"club:{'manage' if owner else 'campaign'}:{c['id']}")])
    prefix = "admin" if owner else "list"
    nav = []
    if result["page"]:
        nav.append(b("⬅️", f"club:{prefix}:{result['page'] - 1}"))
    if (result["page"] + 1) * 5 < result["total"]:
        nav.append(b("➡️", f"club:{prefix}:{result['page'] + 1}"))
    if nav:
        rows.append(nav)
    if owner:
        rows += [[b("➕ Создать кампанию", "club:new")], [b("📝 Продолжить мастер", "club:resume")]]
    rows.append(back())
    return "\n".join(lines), kb(rows)


def campaign_view(app, uid, cid):
    c = app.club.campaign(uid, cid)
    lines = [
        f"📣 <b>{e(c['title'], 80)}</b>",
        e(c["description"], 900),
        "",
        status(c, app.club._now()),
        f"🎯 Твоя цель: {c['my_qualified']}/{c['goal']} подтверждений; по достижении — значок кампании.",
        f"✨ За подтверждённого друга: {c['reward']} клубных баллов.",
        f"📦 Лимит зачётов: {c['qualified_count']}/{c['quota']} · срок: {end_label(c)}.",
        "",
        "Зачёт получает новый участник по персональной ссылке: знакомство, задание и подтверждённый доступ к боту.",
        "Места назначаются при зачёте; переход по ссылке не резервирует место.",
    ]
    answer = c["my_answer"]
    if answer and answer["answered_at"] is not None:
        lines.append("\n✅ Твоё задание выполнено; 10 клубных баллов начислены.")
    rows = [
        [b("🎯 Пройти задание · 10 баллов", f"club:quiz:{cid}")],
        [b("🔗 Моя ссылка кампании", f"club:link:{cid}")],
        [b("👋 Знакомство", "club:onboard")],
        [b("📣 Все кампании", "club:list:0")],
        back(),
    ]
    return "\n".join(lines), kb(rows)


def quiz_view(app, uid, cid):
    c = app.club.open_quiz(uid, cid)
    text = (
        f"🎯 <b>{e(c['title'], 80)}</b>\n{e(c['description'], 900)}\n\n<b>{e(c['question'], 180)}</b>\n\n"
        + "\n".join(f"{i + 1}. {e(answer, 80)}" for i, answer in enumerate(c["answers"]))
        + "\n\n"
        "После ошибки — пауза 30 сек., после трёх ошибок — 24 часа. Награда за верный ответ: 10 клубных баллов один раз."
    )
    rows = [[b(f"{i + 1}. {answer[:60]}", f"club:answer:{cid}:{i}:{c['revision']}")] for i, answer in enumerate(c["answers"])]
    return text, kb(rows + [[b("📣 К кампании", f"club:campaign:{cid}")], back()])


def invite_list(app, uid, page):
    result = app.club.invites(uid, page)
    lines = ["👥 <b>Мои приглашения в кампании</b>", f"Всего: {result['total']} · страница {result['page'] + 1}"]
    for row in result["rows"]:
        label = (
            "✅ Засчитан"
            if row["status"] == "qualified"
            else "⛔ " + (row["reason"] or "Не засчитан")
            if row["status"] == "rejected"
            else "⏳ Ждёт знакомства"
            if not row["onboarding_complete"]
            else "⏳ Ждёт задания"
            if row["answered_at"] is None
            else "⏳ Ждёт подтверждения доступа"
        )
        if row["status"] == "joined" and row["ends_at"] and app.club._now() >= row["ends_at"]:
            label = "⌛ Срок кампании истёк"
        lines.extend(("", f"<b>{e(row['full_name'] or 'Участник', 40)}</b> · {e(row['title'], 50)}", e(label, 100)))
    if not result["rows"]:
        lines.append("Создай ссылку кампании и пригласи заинтересованных друзей. Здесь будут видны их шаги.")
    nav = []
    if result["page"]:
        nav.append(b("⬅️", f"club:invites:{result['page'] - 1}"))
    if (result["page"] + 1) * 8 < result["total"]:
        nav.append(b("➡️", f"club:invites:{result['page'] + 1}"))
    return "\n".join(lines), kb(([nav] if nav else []) + [[b("📣 Выбрать кампанию", "club:list:0")], back()])


def achievements(app, uid):
    s = app.club.summary(uid)
    lines = ["🏅 <b>Мои достижения</b>"] + [e(row["title"], 100) for row in s["badges"][-8:]]
    lines.extend(
        ("", "Следующие ориентиры: 1, 3 и 10 подтверждённых друзей; личная цель каждой кампании.", "", "📜 Последние начисления:")
    )
    names = {"onboarding": "Знакомство", "quiz": "Задание кампании", "referral": "Подтверждённый друг"}
    for row in s["history"]:
        lines.append(f"{names[row['kind']]} · +{row['amount']} клубных баллов")
    return "\n".join(lines), kb([[b("👋 Знакомство", "club:onboard"), b("📣 Кампании", "club:list:0")],
                                [b("🏅 Командные значки", "rt:awards:0")], back()])


def ranking(app):
    lines = ["🏆 <b>Рейтинг клуба</b>", "Сначала подтверждённые друзья, затем клубные баллы."]
    for i, row in enumerate(app.club.ranking(), 1):
        lines.append(f"{i}. {e(row['full_name'] or 'Участник', 40)} — {row['qualified']} 👥 · {row['points']} баллов")
    return "\n".join(lines), kb([back()])


def owner_card(app, uid, cid, *, confirm=False):
    c = app.club.owner_campaign(uid, cid)
    text = (
        f"⚙️ <b>{e(c['title'], 80)}</b>\n{e(c['description'], 900)}\n\n{status(c, app.club._now())}\n"
        f"Цель: {c['goal']} подтверждений · награда: {c['reward']} клубных баллов за друга.\n"
        f"Лимит: {c['qualified_count']}/{c['quota']} · длительность: {c['days']} дн. · срок: {end_label(c)}\n\n"
        f"<b>Задание:</b> {e(c['question'], 180)}\n"
        + "\n".join(f"{'✅' if i == c['correct'] else '▫️'} {e(answer, 80)}" for i, answer in enumerate(c["answers"]))
        + "\n\n<i>Баллы не являются денежной выплатой. После публикации условия закреплены; для новых условий создайте копию.</i>"
    )
    rows = []
    if c["status"] == "draft":
        if confirm:
            text += "\n\nПодтвердите публикацию: кампания станет доступна участникам, срок начнётся сейчас."
            rows.append([b("✅ Подтвердить публикацию", f"club:publish:{cid}:{c['revision']}")])
        else:
            rows.append([b("📣 Опубликовать", f"club:publish_view:{cid}:{c['revision']}")])
            rows.append([b("✏️ Изменить условия", f"club:edit:{cid}:{c['revision']}")])
    elif c["status"] == "active":
        rows.append([b("⏸ Приостановить новые входы", f"club:pause:{cid}:{c['revision']}")])
    else:
        rows.append([b("▶️ Возобновить входы", f"club:activate:{cid}:{c['revision']}")])
    rows += [[b("📋 Скопировать в черновик", f"club:copy:{cid}")], [b("⚙️ Все кампании", "club:admin:0")], back()]
    return text, kb(rows)


def wizard_view(app, uid):
    draft = app.club.wizard_get(uid)
    if not draft:
        raise ClubError("Сохранённого мастера нет. Нажмите «Создать кампанию».")
    if draft["step"] == "confirm":
        d = draft["data"]
        text = (
            f"📝 <b>Предпросмотр кампании</b>\n{e(d['title'], 80)}\n{e(d['description'], 900)}\n\n"
            f"Цель: {d['goal']} · за друга: {d['reward']} клубных баллов · лимит: {d['quota']}\n"
            f"Срок: {d['days']} дн. после публикации\n\n<b>{e(d['question'], 180)}</b>\n"
            + "\n".join(f"{'✅' if i == d['correct'] else '▫️'} {e(a, 80)}" for i, a in enumerate(d["answers"]))
            + "\n\nСохранение создаёт черновик. Публикация подтверждается отдельно."
        )
        rows = [[b("💾 Сохранить черновик", f"club:save:{draft['version']}")]]
        fields = [
            b("✏️ " + label, f"club:field:{field}:{draft['version']}")
            for field, label in zip(WIZARD_FIELDS, FIELD_LABELS, strict=True)
        ]
        rows.extend(fields[i : i + 2] for i in range(0, len(fields), 2))
    else:
        step = WIZARD_FIELDS.index(draft["step"]) + 1
        text = f"📝 <b>Новая кампания · шаг {step}/9</b>\n{PROMPTS[draft['step']]}\n\n/cancel — удалить этот черновик мастера."
        rows = []
        if draft["step"] == "correct":
            text += "\n\n" + "\n".join(f"{i + 1}. {e(answer, 80)}" for i, answer in enumerate(draft["data"]["answers"]))
            rows = [
                [b(f"{i + 1}. {answer[:60]}", f"club:correct:{i}:{draft['version']}")]
                for i, answer in enumerate(draft["data"]["answers"])
            ]
    rows += [[b("⏸ Отложить", "club:later"), b("🗑 Удалить черновик мастера", "club:discard")]]
    return text, kb(rows)


class ActiveDraft(Filter):
    def __init__(self, app):
        self.app = app

    async def __call__(self, message: Message):
        if message.chat.type != "private" or not message.from_user or not is_owner(self.app, message.from_user.id):
            return False
        draft = self.app.club.wizard_get(message.from_user.id)
        return bool(draft and draft["active"])


async def refresh_access(app, cb):
    result = await app.gate_result(cb.bot, cb.from_user.id)
    if result.confirmed:
        app.club.confirm_access(cb.from_user.id)
        return True
    app.club.clear_access(cb.from_user.id)
    return False


def build_router(app):
    r = Router(name="referral-club")

    @r.message(F.text == BTN_CLUB)
    async def open_club(message: Message, state: FSMContext):
        if message.chat.type != "private":
            await message.answer("Откройте клуб в личном чате с ботом.")
            return
        app.template.pause_forms(app, message.from_user.id)
        try:
            text, markup = home(app, message.from_user.id)
        except ClubError as exc:
            await message.answer(str(exc))
            return
        await state.clear()
        await message.answer(text, reply_markup=markup)

    @r.callback_query(F.data.startswith("club:"))
    async def action(cb: CallbackQuery, state: FSMContext):
        if not isinstance(cb.message, Message) or cb.message.chat.type != "private" or cb.message.chat.id != cb.from_user.id:
            await cb.answer("Откройте клуб в личном чате с ботом.", show_alert=True)
            return
        uid, parts, note = cb.from_user.id, cb.data.split(":"), ""
        kind = parts[1] if len(parts) > 1 else ""
        try:
            if kind in ADMIN_ACTIONS and not is_owner(app, uid):
                raise ClubError("Доступно только текущему владельцу бота.")
            if cb.data == "club:home":
                text, markup = home(app, uid)
            elif cb.data == "club:onboard":
                text, markup = onboarding(app, uid)
            elif len(parts) == 4 and kind == "interest":
                app.club.choose_interest(uid, parts[2], number(parts[3]))
                text, markup = onboarding(app, uid)
            elif len(parts) == 4 and kind == "onanswer":
                await refresh_access(app, cb)
                app.club.finish_onboarding(uid, number(parts[2]), number(parts[3]))
                note = "👋 Знакомство завершено! +20 клубных баллов"
                text, markup = home(app, uid)
            elif len(parts) == 3 and kind in {"list", "admin"}:
                text, markup = campaign_list(app, uid, number(parts[2]), kind == "admin")
            elif len(parts) == 3 and kind == "campaign":
                text, markup = campaign_view(app, uid, number(parts[2]))
            elif len(parts) == 3 and kind == "quiz":
                text, markup = quiz_view(app, uid, number(parts[2]))
            elif len(parts) == 5 and kind == "answer":
                await refresh_access(app, cb)
                result = app.club.answer(uid, number(parts[2]), number(parts[3]), number(parts[4]))
                if not result["correct"]:
                    await cb.answer("Ответ пока неверный. Изучите описание и повторите после паузы.", show_alert=True)
                    return
                note = "✅ Задание выполнено! +10 клубных баллов"
                text, markup = campaign_view(app, uid, number(parts[2]))
            elif len(parts) == 3 and kind == "link":
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", app.username):
                    raise ClubError("Ссылка пока недоступна: имя бота не подтверждено.")
                token = app.club.personal_link(uid, number(parts[2]))
                text = (
                    "🔗 <b>Твоя ссылка кампании</b>\nПриглашай тех, кому интересна задача кампании.\n\n"
                    f"https://t.me/{app.username}?start=c_{token}\n\nЗачёт — после знакомства, задания и проверки доступа."
                )
                markup = kb([[b("📣 К кампании", f"club:campaign:{parts[2]}")], back()])
            elif len(parts) == 3 and kind == "invites":
                text, markup = invite_list(app, uid, number(parts[2]))
            elif cb.data == "club:achievements":
                text, markup = achievements(app, uid)
            elif cb.data == "club:rank":
                text, markup = ranking(app)
            elif cb.data == "club:check":
                confirmed = await refresh_access(app, cb)
                note = "Доступ подтверждён. Статус обновлён." if confirmed else "Доступ пока не подтверждён. Проверьте позже."
                text, markup = home(app, uid)
            elif len(parts) == 3 and kind == "manage":
                text, markup = owner_card(app, uid, number(parts[2]))
            elif len(parts) == 4 and kind == "edit":
                app.club.wizard_edit_campaign(uid, number(parts[2]), number(parts[3]))
                text, markup = wizard_view(app, uid)
            elif len(parts) == 4 and kind == "field":
                app.club.wizard_edit_field(uid, parts[2], number(parts[3]))
                text, markup = wizard_view(app, uid)
            elif kind in {"new", "resume"} and len(parts) == 2:
                if kind == "resume" and not app.club.wizard_get(uid):
                    raise ClubError("Сохранённого мастера нет. Нажмите «Создать кампанию».")
                app.club.wizard_begin(uid)
                text, markup = wizard_view(app, uid)
            elif len(parts) == 4 and kind == "correct":
                app.club.wizard_input(uid, number(parts[2]), expected_version=number(parts[3]))
                text, markup = wizard_view(app, uid)
            elif len(parts) == 3 and kind == "save":
                cid = app.club.wizard_save(uid, number(parts[2]))
                text, markup = owner_card(app, uid, cid)
                note = "Черновик сохранён"
            elif len(parts) == 4 and kind == "publish_view":
                c = app.club.owner_campaign(uid, number(parts[2]))
                if c["revision"] != number(parts[3]):
                    raise ClubError("Карточка устарела. Откройте кампанию заново.")
                text, markup = owner_card(app, uid, c["id"], confirm=True)
            elif len(parts) == 4 and kind in {"publish", "pause", "activate"}:
                cid, revision = number(parts[2]), number(parts[3])
                if kind == "publish":
                    app.club.publish(uid, cid, revision)
                else:
                    app.club.set_status(uid, cid, "paused" if kind == "pause" else "active", revision)
                text, markup = owner_card(app, uid, cid)
            elif len(parts) == 3 and kind == "copy":
                text, markup = owner_card(app, uid, app.club.copy_draft(uid, number(parts[2])))
            elif cb.data in {"club:later", "club:discard"}:
                if kind == "discard":
                    app.club.wizard_discard(uid)
                else:
                    app.club.wizard_pause(uid)
                text, markup = campaign_list(app, uid, owner=True)
            else:
                raise ClubError("Кнопка устарела. Откройте клуб заново.")
        except ClubError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        if kind not in WRITING_ACTIONS and is_owner(app, uid):
            app.club.wizard_pause(uid)
        app.teams.form_pause(uid)
        await state.clear()
        await cb.answer(note)
        try:
            await cb.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                await cb.message.answer(text, reply_markup=markup, disable_web_page_preview=True)

    @r.message(F.text, ActiveDraft(app))
    async def draft_input(message: Message):
        uid = message.from_user.id
        try:
            draft = app.club.wizard_get(uid)
            if draft["step"] in {"correct", "confirm"}:
                await message.answer("Выберите действие кнопкой ниже.")
            else:
                app.club.wizard_input(uid, message.text)
            text, markup = wizard_view(app, uid)
            await message.answer(text, reply_markup=markup)
        except ClubError as exc:
            await message.answer(str(exc))

    return r
