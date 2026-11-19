"""Шаблон «Задания + проверка»: владелец создаёт задания, пользователи выполняют и получают награду после проверки."""
from __future__ import annotations

import html
import json
import math

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.db import fmt, now
from child.templates import Setting, TemplateInfo
from child.withdrawals import is_owner

INFO = TemplateInfo(
    key="microtasks",
    title="Задания + проверка",
    emoji="✅",
    price_key="PRICE_MICROTASKS",
    description="Владелец создаёт задания с разными типами доказательств. "
                "Пользователи выполняют их и получают награду после ручной или автоматической проверки. "
                "Есть очередь заявок, комментарии при отклонении и лимит активных заявок.",
    settings=[
        Setting("welcome", "Приветствие", "Выполняй задания и получай награду после проверки. "
                "Владелец рассматривает заявки вручную или настраивает авто-одобрение."),
        Setting("currency", "Валюта", "₽"),
        Setting("ref_reward", "Награда за друга", 5, "float"),
        Setting("min_withdraw", "Мин. вывод", 100, "float"),
        Setting("withdraw_details_prompt", "Текст запроса реквизитов", "Укажите номер карты или кошелька:"),
        Setting("required_channel", "Обязательный канал", "", "channel"),
        Setting("channel_url", "Ссылка на канал (для приватных)", "-"),
        Setting("max_pending", "Макс. активных заявок", 3, "int"),
        Setting("auto_reject_hours", "Авто-отклонение через (часов)", 48, "int"),
    ],
    version=1,
    capabilities=("microtasks", "manual_review", "auto_approve"),
)

BTN_TASKS = "📋 Задания"
BTN_MY = "📄 Мои задания"
BTN_TOP = "🏆 Топ"
BTN_INVITE = "👥 Пригласить"
ADMIN_BUTTONS = [("📋 Задания", "mt:admin:tasks"), ("✅ Очередь", "mt:admin:queue:0")]


class TaskCreate(StatesGroup):
    title = State()
    description = State()
    reward = State()
    proof_types = State()
    approval = State()
    repeat = State()
    cooldown = State()
    preview = State()


class SubmitProof(StatesGroup):
    waiting = State()


def menu_rows(app) -> list[list[str]]:
    return [[BTN_TASKS, BTN_MY], [BTN_TOP, BTN_INVITE]]


def _can_do_task(app, uid: int, task) -> tuple[bool, str]:
    """Проверяет, может ли пользователь выполнить задание сейчас."""
    if task["repeat_mode"] == "once":
        done = app.db.one(
            "SELECT 1 FROM micro_submissions WHERE user_id=? AND task_id=? AND status='approved'",
            uid, task["id"]
        )
        if done:
            return False, "Это задание можно выполнить только один раз."
    elif task["repeat_mode"] == "repeatable" and task["cooldown_hours"] > 0:
        last = app.db.one(
            "SELECT created_at FROM micro_submissions WHERE user_id=? AND task_id=? AND status='approved' "
            "ORDER BY id DESC LIMIT 1",
            uid, task["id"]
        )
        if last:
            elapsed = now() - last["created_at"]
            need = task["cooldown_hours"] * 3600
            if elapsed < need:
                left = (need - elapsed) // 60
                return False, f"Кулдаун: ещё примерно {left} мин."
    pending = app.db.scalar(
        "SELECT COUNT(*) FROM micro_submissions WHERE user_id=? AND status='pending'", uid
    ) or 0
    if pending >= int(app.s.get("max_pending") or 3):
        return False, f"У вас уже {pending} активных заявок."
    return True, ""


_PROOF_LABELS = {"text": "текст", "photo": "фото", "link": "ссылка", "button": "кнопка"}


def _proof_types_label(types: list[str]) -> str:
    return ", ".join(_PROOF_LABELS.get(t, t) for t in types) or "—"


def _validate_proof(message: Message, allowed: list[str]) -> str | None:
    """Возвращает proof или None, если тип не подходит."""
    if "photo" in allowed and message.photo:
        return message.photo[-1].file_id
    text = (message.text or "").strip()
    if not text:
        return None
    if "link" in allowed and text.startswith(("http://", "https://")):
        return text[:2000]
    if "text" in allowed:
        return text[:2000]
    if "button" in allowed and text.lower() in {"выполнил", "готово", "done", "ok"}:
        return "button"
    return None


def build_router(app) -> Router:
    r = Router()

    @r.message(F.text == BTN_TASKS)
    async def tasks_list(message: Message) -> None:
        uid = message.from_user.id
        rows = app.db.all("SELECT * FROM micro_tasks WHERE is_active=1 ORDER BY id DESC LIMIT 30")
        available = []
        for t in rows:
            ok, _ = _can_do_task(app, uid, t)
            if ok:
                available.append(t)
        if not available:
            await message.answer("Сейчас нет доступных заданий (или достигнут лимит заявок).")
            return
        lines = ["📋 <b>Доступные задания</b>\n"]
        kb = []
        for t in available:
            types = json.loads(t["proof_types"] or "[]")
            lines.append(
                f"• {html.escape(t['title'])} — {fmt(t['reward'])} {app.s['currency']}"
                f" ({_proof_types_label(types)})"
            )
            kb.append([InlineKeyboardButton(text=f"✅ {t['title'][:28]}", callback_data=f"mt:do:{t['id']}")])
        await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

    @r.callback_query(F.data.startswith("mt:do:"))
    async def start_do(cb: CallbackQuery, state: FSMContext) -> None:
        tid = int(cb.data.split(":")[-1])
        task = app.db.one("SELECT * FROM micro_tasks WHERE id=? AND is_active=1", tid)
        if not task:
            await cb.answer("Задание недоступно", show_alert=True)
            return
        ok, reason = _can_do_task(app, cb.from_user.id, task)
        if not ok:
            await cb.answer(reason, show_alert=True)
            return
        await state.set_state(SubmitProof.waiting)
        await state.update_data(task_id=tid)
        types = json.loads(task["proof_types"] or "[]")
        await cb.message.answer(
            f"Задание: <b>{html.escape(task['title'])}</b>\n"
            f"Награда: {fmt(task['reward'])} {app.s['currency']}\n"
            f"Доказательства: {_proof_types_label(types)}\n\n"
            "Пришлите подходящее доказательство или /cancel."
        )
        await cb.answer()

    @r.message(SubmitProof.waiting)
    async def got_proof(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        tid = data.get("task_id")
        if not tid:
            await state.clear()
            return
        task = app.db.one("SELECT * FROM micro_tasks WHERE id=? AND is_active=1", tid)
        if not task:
            await state.clear()
            await message.answer("Задание больше недоступно.")
            return
        ok, reason = _can_do_task(app, message.from_user.id, task)
        if not ok:
            await state.clear()
            await message.answer(reason)
            return
        types = json.loads(task["proof_types"] or "[]")
        proof = _validate_proof(message, types)
        if not proof:
            await message.answer(f"Нужно доказательство одного из типов: {_proof_types_label(types)}")
            return
        uid = message.from_user.id
        status = "approved" if task["approval_mode"] == "auto" else "pending"
        app.db.exec(
            "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES(?,?,?,?,?)",
            uid, tid, proof, status, now()
        )
        if status == "approved":
            app.db.add_balance(uid, task["reward"])
            await message.answer(f"Задание выполнено! Начислено {fmt(task['reward'])} {app.s['currency']}.")
        else:
            await message.answer("Заявка отправлена на проверку.")
            # Уведомление владельцу
            try:
                await message.bot.send_message(
                    app.owner_id,
                    f"Новая заявка на задание «{html.escape(task['title'])}» от пользователя {uid}."
                )
            except Exception:
                pass
        await state.clear()

    @r.message(F.text == BTN_MY)
    async def my_submissions(message: Message) -> None:
        rows = app.db.all(
            "SELECT s.*, t.title FROM micro_submissions s "
            "JOIN micro_tasks t ON t.id=s.task_id WHERE s.user_id=? ORDER BY s.id DESC LIMIT 15",
            message.from_user.id
        )
        if not rows:
            await message.answer("У вас пока нет заявок.")
            return
        lines = ["📄 <b>Мои задания</b>\n"]
        for s in rows:
            lines.append(
                f"• {html.escape(s['title'])} — {s['status']}"
                + (f"\n  Комментарий: {html.escape(s['comment'])}" if s["comment"] else "")
            )
        await message.answer("\n".join(lines))

    @r.message(F.text == BTN_TOP)
    async def top(message: Message) -> None:
        rows = app.db.all(
            "SELECT u.full_name, COUNT(*) cnt FROM micro_submissions s "
            "JOIN users u ON u.id=s.user_id WHERE s.status='approved' "
            "GROUP BY s.user_id ORDER BY cnt DESC LIMIT 10"
        )
        lines = ["🏆 <b>Топ по выполненным заданиям</b>\n"]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {html.escape(row['full_name'] or '—')} — {row['cnt']}")
        if len(lines) == 1:
            lines.append("Пока пусто.")
        await message.answer("\n".join(lines))

    @r.message(F.text == BTN_INVITE)
    async def invite(message: Message) -> None:
        me = await message.bot.get_me()
        link = f"https://t.me/{me.username}?start={message.from_user.id}"
        reward = app.s.get("ref_reward") or 0
        await message.answer(
            f"Приглашайте друзей по ссылке:\n<code>{link}</code>\n\n"
            f"Награда за друга: {fmt(reward)} {app.s['currency']}"
        )

    return r


def build_admin_router(app) -> Router:
    r = Router()

    def tasks_kb():
        rows = app.db.all("SELECT id, title, is_active FROM micro_tasks ORDER BY id DESC LIMIT 30")
        kb = [[InlineKeyboardButton(text="➕ Создать задание", callback_data="mt:create")]]
        for t in rows:
            mark = "✅" if t["is_active"] else "⏸"
            kb.append([InlineKeyboardButton(text=f"{mark} {t['title'][:40]}", callback_data=f"mt:toggle:{t['id']}")])
        return InlineKeyboardMarkup(inline_keyboard=kb)

    @r.callback_query(F.data == "mt:admin:tasks")
    async def admin_tasks(cb: CallbackQuery) -> None:
        if not is_owner(app, cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        await cb.message.answer("📋 <b>Задания</b>", reply_markup=tasks_kb())
        await cb.answer()

    @r.callback_query(F.data == "mt:create")
    async def start_create(cb: CallbackQuery, state: FSMContext) -> None:
        if not is_owner(app, cb.from_user.id):
            return
        await state.set_state(TaskCreate.title)
        await cb.message.answer("Название задания:")
        await cb.answer()

    @r.message(TaskCreate.title)
    async def step_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title=message.text[:200])
        await state.set_state(TaskCreate.description)
        await message.answer("Описание (или - чтобы пропустить):")

    @r.message(TaskCreate.description)
    async def step_desc(message: Message, state: FSMContext) -> None:
        desc = "" if message.text == "-" else message.text[:1000]
        await state.update_data(description=desc)
        await state.set_state(TaskCreate.reward)
        await message.answer("Награда (число):")

    @r.message(TaskCreate.reward)
    async def step_reward(message: Message, state: FSMContext) -> None:
        try:
            reward = float(message.text.replace(",", "."))
            if not math.isfinite(reward) or reward <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Введите положительное число.")
            return
        await state.update_data(reward=reward)
        await state.set_state(TaskCreate.proof_types)
        await message.answer(
            "Типы доказательств через запятую: text, photo, link, button\n"
            "Пример: text,photo"
        )

    @r.message(TaskCreate.proof_types)
    async def step_types(message: Message, state: FSMContext) -> None:
        allowed = {"text", "photo", "link", "button"}
        types = [t.strip() for t in message.text.split(",") if t.strip() in allowed]
        if not types:
            await message.answer("Укажите хотя бы один тип из: text, photo, link, button")
            return
        await state.update_data(proof_types=types)
        await state.set_state(TaskCreate.approval)
        await message.answer("Режим проверки: manual или auto")

    @r.message(TaskCreate.approval)
    async def step_approval(message: Message, state: FSMContext) -> None:
        mode = message.text.strip().lower()
        if mode not in {"manual", "auto"}:
            await message.answer("Напишите manual или auto")
            return
        await state.update_data(approval_mode=mode)
        await state.set_state(TaskCreate.repeat)
        await message.answer("Повторяемость: once или repeatable")

    @r.message(TaskCreate.repeat)
    async def step_repeat(message: Message, state: FSMContext) -> None:
        mode = message.text.strip().lower()
        if mode not in {"once", "repeatable"}:
            await message.answer("Напишите once или repeatable")
            return
        await state.update_data(repeat_mode=mode)
        if mode == "repeatable":
            await state.set_state(TaskCreate.cooldown)
            await message.answer("Кулдаун в часах (0 — без кулдауна):")
        else:
            await state.update_data(cooldown_hours=0)
            await state.set_state(TaskCreate.preview)
            await _show_preview(message, state)

    @r.message(TaskCreate.cooldown)
    async def step_cooldown(message: Message, state: FSMContext) -> None:
        try:
            hours = int(message.text)
            if hours < 0:
                raise ValueError
        except ValueError:
            await message.answer("Введите целое число ≥ 0")
            return
        await state.update_data(cooldown_hours=hours)
        await state.set_state(TaskCreate.preview)
        await _show_preview(message, state)

    async def _show_preview(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        text = (
            f"<b>Предпросмотр</b>\n"
            f"Название: {html.escape(data['title'])}\n"
            f"Описание: {html.escape(data.get('description') or '—')}\n"
            f"Награда: {fmt(data['reward'])}\n"
            f"Доказательства: {_proof_types_label(data['proof_types'])}\n"
            f"Проверка: {data['approval_mode']}\n"
            f"Повтор: {data['repeat_mode']}"
            + (f" (кулдаун {data.get('cooldown_hours', 0)} ч)" if data['repeat_mode'] == 'repeatable' else "")
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💾 Сохранить", callback_data="mt:save")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="mt:cancel")],
        ])
        await message.answer(text, reply_markup=kb)

    @r.callback_query(F.data == "mt:save", StateFilter(TaskCreate.preview))
    async def save_task(cb: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        app.db.exec(
            """INSERT INTO micro_tasks
               (title, description, reward, proof_types, approval_mode, repeat_mode, cooldown_hours, is_active, created_at)
               VALUES (?,?,?,?,?,?,?,1,?)""",
            data["title"], data.get("description") or "", data["reward"],
            json.dumps(data["proof_types"]), data["approval_mode"], data["repeat_mode"],
            data.get("cooldown_hours", 0), now()
        )
        await state.clear()
        await cb.message.answer("Задание создано.", reply_markup=tasks_kb())
        await cb.answer()

    @r.callback_query(F.data == "mt:cancel")
    async def cancel_create(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await cb.message.answer("Создание отменено.")
        await cb.answer()

    @r.callback_query(F.data.startswith("mt:toggle:"))
    async def toggle(cb: CallbackQuery) -> None:
        tid = int(cb.data.split(":")[-1])
        app.db.exec("UPDATE micro_tasks SET is_active = 1 - is_active WHERE id=?", tid)
        await cb.message.edit_reply_markup(reply_markup=tasks_kb())
        await cb.answer()

    @r.callback_query(F.data.startswith("mt:admin:queue:"))
    async def queue(cb: CallbackQuery) -> None:
        if not is_owner(app, cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        # Авто-отклонение старых заявок
        hours = int(app.s.get("auto_reject_hours") or 48)
        if hours > 0:
            cutoff = now() - hours * 3600
            app.db.exec(
                "UPDATE micro_submissions SET status='rejected', comment='Авто-отклонение по сроку', decided_at=? "
                "WHERE status='pending' AND created_at < ?",
                now(), cutoff
            )
        page = int(cb.data.split(":")[-1])
        rows = app.db.all(
            "SELECT s.*, t.title, u.full_name FROM micro_submissions s "
            "JOIN micro_tasks t ON t.id=s.task_id "
            "JOIN users u ON u.id=s.user_id "
            "WHERE s.status='pending' ORDER BY s.id LIMIT 10 OFFSET ?",
            page * 10
        )
        if not rows:
            await cb.message.answer("Очередь пуста.")
            await cb.answer()
            return
        for s in rows:
            text = (
                f"Заявка #{s['id']}\n"
                f"Пользователь: {html.escape(s['full_name'] or str(s['user_id']))}\n"
                f"Задание: {html.escape(s['title'])}\n"
                f"Доказательство: {html.escape(str(s['proof'])[:500])}"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Одобрить", callback_data=f"mt:ok:{s['id']}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mt:no:{s['id']}"),
                ]
            ])
            await cb.message.answer(text, reply_markup=kb)
        await cb.answer()

    @r.callback_query(F.data.startswith("mt:ok:"))
    async def approve(cb: CallbackQuery) -> None:
        sid = int(cb.data.split(":")[-1])
        sub = app.db.one(
            "SELECT s.*, t.reward FROM micro_submissions s "
            "JOIN micro_tasks t ON t.id=s.task_id WHERE s.id=? AND s.status='pending'",
            sid
        )
        if not sub:
            await cb.answer("Уже обработано", show_alert=True)
            return
        app.db.exec("UPDATE micro_submissions SET status='approved', decided_at=? WHERE id=?", now(), sid)
        app.db.add_balance(sub["user_id"], sub["reward"])
        await cb.message.edit_text(cb.message.text + "\n\n✅ Одобрено")
        await cb.answer("Одобрено")

    @r.callback_query(F.data.startswith("mt:no:"))
    async def reject_start(cb: CallbackQuery, state: FSMContext) -> None:
        sid = int(cb.data.split(":")[-1])
        await state.update_data(reject_id=sid)
        await state.set_state("mt_reject_comment")
        await cb.message.answer("Комментарий к отклонению (или - чтобы без комментария):")
        await cb.answer()

    @r.message(StateFilter("mt_reject_comment"))
    async def reject_finish(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        sid = data.get("reject_id")
        comment = "" if message.text == "-" else (message.text or "")[:500]
        app.db.exec(
            "UPDATE micro_submissions SET status='rejected', comment=?, decided_at=? WHERE id=? AND status='pending'",
            comment, now(), sid
        )
        await state.clear()
        await message.answer("Заявка отклонена.")

    return r
