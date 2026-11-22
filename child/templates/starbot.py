"""Шаблон «Звёздный бот»: звёзды за рефералов, ежедневный бонус, задания, вывод."""
from __future__ import annotations

import html
import math
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.db import fmt
from child.templates import Setting, TemplateInfo
from child.withdrawals import is_owner

INFO = TemplateInfo(
    key="starbot",
    title="Звёздный бот",
    emoji="🌟",
    price_key="PRICE_STARBOT",
    description="Пользователи зарабатывают звёзды за друзей, ежедневный бонус и задания "
                "(подписки на каналы) и подают заявки на вывод Telegram Stars.",
    settings=[
        Setting("welcome", "Приветствие", "🌟 Зарабатывай звёзды за друзей, бонусы и задания и выводи их!"),
        Setting("currency", "Валюта", "⭐"),
        Setting("ref_reward", "Звёзд за друга", 1, "float"),
        Setting("daily_bonus", "Ежедневный бонус", 0.5, "float"),
        Setting("min_withdraw", "Мин. вывод", 15, "float"),
        Setting("withdraw_details_prompt", "Текст запроса реквизитов",
                "Укажите @username, на который отправить звёзды (подарком):"),
        Setting("required_channel", "Обязательный канал", "", "channel"),
        Setting("channel_url", "Ссылка на канал (для приватных)", "-"),
    ],
)

BTN_EARN = "⭐ Заработать звёзды"
BTN_BONUS = "🎁 Ежедневный бонус"
BTN_TASKS = "📋 Задания"
BTN_TOP = "🏆 Топ"

DAY = 86400

ADMIN_BUTTONS = [("📋 Задания", "adm:tasks")]


class TaskAdd(StatesGroup):
    waiting = State()  # step 1: title (or the legacy one-line format)
    url = State()
    reward = State()
    channel = State()
    confirm = State()


def menu_rows(app) -> list[list[str]]:
    return [[BTN_EARN, BTN_BONUS], [BTN_TASKS, BTN_TOP]]


def check_title(text: str) -> str:
    title = (text or "").strip()
    if not title:
        raise ValueError("title")
    return title[:100]


def check_url(text: str) -> str:
    url = (text or "").strip()
    if not url.startswith(("http://", "https://", "tg://")):
        raise ValueError("url")
    return url


def check_reward(text: str) -> float:
    reward = float((text or "").strip().replace(",", "."))
    if not math.isfinite(reward) or reward <= 0:
        raise ValueError("reward")
    return reward


def check_channel(text: str) -> str:
    channel = (text or "").strip()
    if channel and not (channel.startswith("@") or channel.lstrip("-").isdigit()):
        raise ValueError("channel")
    return channel


def parse_task(text: str) -> tuple[str, str, float, str]:
    """«Название | ссылка | награда | @канал(необязательно)»."""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 3:
        raise ValueError("format")
    return (check_title(parts[0]), check_url(parts[1]), check_reward(parts[2]),
            check_channel(parts[3] if len(parts) > 3 else ""))


def build_router(app) -> Router:
    r = Router(name="starbot")

    @r.message(F.text == BTN_EARN)
    async def earn(message: Message) -> None:
        uid = message.from_user.id
        await message.answer(
            f"⭐ Приглашайте друзей и получайте <b>{fmt(float(app.s['ref_reward']))} {app.currency}</b> за каждого!\n\n"
            f"Приглашено: {app.db.referrals(uid)}\n🔗 {app.ref_link(uid)}",
            disable_web_page_preview=True,
        )

    @r.message(F.text == BTN_BONUS)
    async def bonus(message: Message) -> None:
        uid = message.from_user.id
        u = app.db.user(uid)
        if not u:
            return
        now = int(time.time())
        left = u["last_daily"] + DAY - now
        if left > 0:
            await message.answer(f"⏳ Следующий бонус через {left // 3600} ч {left % 3600 // 60} мин.")
            return
        amount = float(app.s["daily_bonus"])
        cur = app.db.exec("UPDATE users SET last_daily=?, balance=balance+? WHERE id=? AND last_daily<=?",
                          now, amount, uid, now - DAY)
        if cur.rowcount:
            await message.answer(f"🎁 Вы получили {fmt(amount)} {app.currency}! Возвращайтесь через 24 часа.")

    def tasks_for(uid: int):
        return app.db.all(
            "SELECT * FROM tasks WHERE active=1 AND id NOT IN (SELECT task_id FROM task_done WHERE user_id=?) "
            "ORDER BY id LIMIT 10", uid,
        )

    @r.message(F.text == BTN_TASKS)
    async def tasks(message: Message) -> None:
        rows = tasks_for(message.from_user.id)
        if not rows:
            await message.answer("📋 Сейчас нет доступных заданий. Загляните позже!")
            return
        for t in rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➡️ Перейти", url=t["url"]),
                InlineKeyboardButton(text="✅ Проверить", callback_data=f"task:{t['id']}"),
            ]])
            await message.answer(f"📋 {html.escape(t['title'])}\nНаграда: {fmt(t['reward'])} {app.currency}",
                                 reply_markup=kb)

    @r.callback_query(F.data.startswith("task:"))
    async def task_check(cb: CallbackQuery) -> None:
        tid = int(cb.data.split(":")[1])
        uid = cb.from_user.id
        t = app.db.one("SELECT * FROM tasks WHERE id=? AND active=1", tid)
        if not t:
            await cb.answer("Задание недоступно", show_alert=True)
            return
        if t["channel"]:
            try:
                m = await cb.bot.get_chat_member(t["channel"], uid)
                if m.status not in {"creator", "administrator", "member"} and not getattr(m, "is_member", False):
                    await cb.answer("❌ Вы не подписаны", show_alert=True)
                    return
            except TelegramBadRequest:
                pass  # бот не админ канала — засчитываем без проверки
        with app.db.lock:
            cur = app.db.exec("INSERT OR IGNORE INTO task_done(user_id, task_id) VALUES(?,?)", uid, tid)
            if cur.rowcount:
                app.db.add_balance(uid, t["reward"])
        if not cur.rowcount:
            await cb.answer("Уже выполнено", show_alert=True)
            return
        await cb.answer(f"✅ +{fmt(t['reward'])} {app.currency}", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

    @r.message(F.text == BTN_TOP)
    async def top(message: Message) -> None:
        rows = app.db.all("SELECT id, full_name, balance FROM users ORDER BY balance DESC LIMIT 10")
        lines = ["🏆 <b>Топ по звёздам</b>\n"]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {html.escape(row['full_name'] or str(row['id']))} — {fmt(row['balance'])} {app.currency}")
        await message.answer("\n".join(lines))

    return r


def build_admin_router(app) -> Router:
    r = Router(name="starbot-admin")
    r.message.filter(F.from_user.id == app.owner_id)
    r.callback_query.filter(F.from_user.id == app.owner_id)

    def tasks_kb() -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text=f"🗑 {t['title'][:30]} ({fmt(t['reward'])})", callback_data=f"tdel:{t['id']}")]
                for t in app.db.all("SELECT * FROM tasks WHERE active=1 ORDER BY id")]
        rows.append([InlineKeyboardButton(text="➕ Добавить задание", callback_data="tadd")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @r.callback_query(F.data == "adm:tasks")
    async def tasks(cb: CallbackQuery) -> None:
        await cb.message.answer("📋 <b>Задания</b> (нажмите, чтобы удалить)", reply_markup=tasks_kb())
        await cb.answer()

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tcancel")]])
    menu_texts = {t for row in menu_rows(app) for t in row} | {"👤 Профиль", "💸 Вывод", "⚙️ Админка"}

    def preview(data: dict) -> tuple[str, InlineKeyboardMarkup]:
        channel = (f"автопроверка подписки на {html.escape(data['channel'])} (бот должен быть админом канала)"
                   if data["channel"] else "без проверки подписки")
        text = ("📋 <b>Проверьте задание</b>\n\n"
                f"Название: {html.escape(data['title'])}\nСсылка: {html.escape(data['url'])}\n"
                f"Награда: {fmt(data['reward'])} {html.escape(app.currency)}\nКанал: {channel}\n\n"
                "Пока вы не нажмёте «Сохранить», ничего не записывается.")
        return text, InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Сохранить", callback_data="tsave"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="tcancel")]])

    async def to_preview(message: Message, state: FSMContext) -> None:
        await state.set_state(TaskAdd.confirm)
        text, kb = preview(await state.get_data())
        await message.answer(text, reply_markup=kb, disable_web_page_preview=True)

    @r.callback_query(F.data == "tadd")
    async def add(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(TaskAdd.waiting)
        await cb.message.answer(
            "➕ <b>Новое задание. Шаг 1/4: название</b>\nКак задание увидят пользователи, например «Подписка на канал».\n"
            "Можно сразу одной строкой: <code>Название | ссылка | награда | @канал</code>\n/cancel — отмена",
            reply_markup=cancel_kb)
        await cb.answer()

    @r.message(StateFilter(TaskAdd.waiting, TaskAdd.url, TaskAdd.reward, TaskAdd.channel), F.text.in_(menu_texts))
    async def menu_during_wizard(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Добавление задания отменено, ничего не сохранено.", reply_markup=app.menu(message.from_user.id))

    @r.message(TaskAdd.waiting, ~Command("cancel"))
    async def step_title(message: Message, state: FSMContext) -> None:
        text = message.text or ""
        if "|" in text:
            try:
                title, url, reward, channel = parse_task(text)
            except ValueError:
                await message.answer("❌ Неверный формат. Пример: <code>Подписка на канал | https://t.me/x | 0.5 | @x</code>\n"
                                     "Или пришлите только название — дальше спрошу по шагам.")
                return
            await state.update_data(title=title, url=url, reward=reward, channel=channel)
            await to_preview(message, state)
            return
        try:
            title = check_title(text)
        except ValueError:
            await message.answer("Шаг 1/4: пришлите название текстом.", reply_markup=cancel_kb)
            return
        await state.update_data(title=title)
        await state.set_state(TaskAdd.url)
        await message.answer("<b>Шаг 2/4: ссылка</b>\nКуда перейти пользователю, например https://t.me/channel.",
                             reply_markup=cancel_kb)

    @r.message(TaskAdd.url, ~Command("cancel"))
    async def step_url(message: Message, state: FSMContext) -> None:
        try:
            url = check_url(message.text or "")
        except ValueError:
            await message.answer("Шаг 2/4: ссылка должна начинаться с http://, https:// или tg://", reply_markup=cancel_kb)
            return
        await state.update_data(url=url)
        await state.set_state(TaskAdd.reward)
        await message.answer(f"<b>Шаг 3/4: награда в {html.escape(app.currency)}</b>\nЧисло больше нуля, например 0.5.",
                             reply_markup=cancel_kb)

    @r.message(TaskAdd.reward, ~Command("cancel"))
    async def step_reward(message: Message, state: FSMContext) -> None:
        try:
            reward = check_reward(message.text or "")
        except ValueError:
            await message.answer(f"Шаг 3/4: нужно число больше нуля ({html.escape(app.currency)}), например 0.5.",
                                 reply_markup=cancel_kb)
            return
        await state.update_data(reward=reward)
        await state.set_state(TaskAdd.channel)
        await message.answer(
            "<b>Шаг 4/4: канал для автопроверки подписки</b>\nПришлите @username или числовой ID канала "
            "(бот должен быть админом канала) — или выполните задание без проверки.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Без проверки подписки", callback_data="tskip")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="tcancel")]]))

    @r.message(TaskAdd.channel, ~Command("cancel"))
    async def step_channel(message: Message, state: FSMContext) -> None:
        try:
            channel = check_channel(message.text or "")
        except ValueError:
            await message.answer("Шаг 4/4: канал — @username или числовой ID, либо «⏭ Без проверки подписки».")
            return
        await state.update_data(channel=channel)
        await to_preview(message, state)

    @r.callback_query(F.data == "tskip")
    async def skip_channel(cb: CallbackQuery, state: FSMContext) -> None:
        if await state.get_state() != TaskAdd.channel.state:
            await cb.answer("Черновик устарел — начните заново.", show_alert=True)
            return
        await cb.answer()
        await state.update_data(channel="")
        await to_preview(cb.message, state)

    @r.callback_query(F.data == "tcancel")
    async def cancel_wizard(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await cb.answer()
        await cb.message.answer("Добавление задания отменено, ничего не сохранено.", reply_markup=tasks_kb())

    @r.callback_query(F.data == "tsave")
    async def save(cb: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if await state.get_state() != TaskAdd.confirm.state or not {"title", "url", "reward", "channel"} <= set(data):
            await cb.answer("Черновик устарел — начните заново.", show_alert=True)
            return
        await state.clear()
        if not is_owner(app, cb.from_user.id):  # fresh check: ownership may have changed during the wizard
            await cb.answer("Нет доступа", show_alert=True)
            return
        app.db.exec("INSERT INTO tasks(title,url,channel,reward) VALUES(?,?,?,?)",
                    data["title"], data["url"], data["channel"], data["reward"])
        await cb.answer("Сохранено")
        await cb.message.answer("✅ Задание добавлено.", reply_markup=tasks_kb())

    @r.callback_query(F.data.startswith("tdel:"))
    async def delete(cb: CallbackQuery) -> None:
        app.db.exec("UPDATE tasks SET active=0 WHERE id=?", int(cb.data.split(":")[1]))
        await cb.message.edit_reply_markup(reply_markup=tasks_kb())
        await cb.answer("Удалено")

    return r
