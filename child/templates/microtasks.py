"""Шаблон «Задания + проверка»: владелец создаёт задания, пользователи выполняют и получают награду после проверки."""
from __future__ import annotations

import html
import json
import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.db import fmt, now
from child.mgram_proxy import check_completion, fetch_pending_result
from child.money import as_minor, to_minor
from child.templates import Setting, TemplateInfo
from child.withdrawals import is_owner

INFO = TemplateInfo(
    key="microtasks",
    title="Задания + проверка",
    emoji="✅",
    price_key="PRICE_MICROTASKS",
    description="Владелец создаёт задания с разными типами доказательств. "
                "Пользователи выполняют их и получают награду после ручной или автоматической проверки. "
                "Есть очередь заявок, комментарии при отклонении и лимит активных заявок. "
                "Опционально можно включить задания MGRAM (по умолчанию выключено).",
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
        Setting("mgram_tasks", "Задания MGRAM (0 — выкл, 1 — вкл)", 0, "int"),
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


def _mgram_enabled(app) -> bool:
    try:
        return int(app.s.get("mgram_tasks") or 0) > 0
    except (TypeError, ValueError):
        return False


def _mgram_bot_id(app) -> int:
    return int(app.cfg.get("bot_id") or 0)


_MGRAM_HINTS = {
    "not_configured": (
        "Задания MGRAM пока недоступны: у конструктора ещё нет ключа хаб-бота. "
        "После перезапуска он пробует получить ключ сам, либо его можно вставить "
        "в «Ключи интеграций»."
    ),
    "no_proxy": (
        "Задания MGRAM пока недоступны: нет связи с конструктором. "
        "Перезапустите этого бота из конструктора."
    ),
    "error": "Задания MGRAM пока недоступны: сервис не ответил. Попробуйте позже.",
    "empty": "Задания MGRAM пока недоступны: сервис вернул пустой ответ. Попробуйте позже.",
    "timeout": "Задания MGRAM пока недоступны: истекло время ожидания. Попробуйте позже.",
    "refused": "Задания MGRAM пока недоступны. Перезапустите этого бота из конструктора.",
}


def _mgram_owner_hint(reason: str) -> str:
    return _MGRAM_HINTS.get(reason, "")


def _callback_int(data: str, *, prefix: str) -> int | None:
    """Parse a non-negative callback id/page. Rejects junk that would crash int()."""
    raw = data[len(prefix):] if data.startswith(prefix) else ""
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _proof_types(task) -> list[str]:
    try:
        types = json.loads(task["proof_types"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(types, list):
        return []
    return [t for t in types if isinstance(t, str)]


def _submit_proof(app, uid: int, tid: int, proof: str) -> tuple[str | None, str, float | None]:
    """Insert a submission and, for auto-approve, credit in the same transaction.

    Returns (status, reason, reward). status is None when the user cannot submit.
    """
    with app.db.tx():
        task = app.db.one("SELECT * FROM micro_tasks WHERE id=? AND is_active=1", tid)
        if not task or not app.db.user(uid):
            return None, "Задание больше недоступно.", None
        ok, reason = _can_do_task(app, uid, task)
        if not ok:
            return None, reason, None
        status = "approved" if task["approval_mode"] == "auto" else "pending"
        try:
            reward = as_minor(task["reward"])
        except ValueError:
            return None, "Задание больше недоступно.", None
        if status == "approved" and reward < 0:
            return None, "Задание больше недоступно.", None
        ts = now()
        if status == "approved":
            app.db.exec(
                "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at, decided_at) "
                "VALUES(?,?,?,?,?,?)",
                uid, tid, proof, status, ts, ts,
            )
            if reward > 0:
                app.db.add_balance(uid, reward)
        else:
            app.db.exec(
                "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES(?,?,?,?,?)",
                uid, tid, proof, status, ts,
            )
        return status, "", reward if status == "approved" else None


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
        lines = ["📋 <b>Доступные задания</b>\n"]
        kb = []
        for t in available:
            types = _proof_types(t)
            lines.append(
                f"• {html.escape(t['title'])} — {fmt(t['reward'])} {app.s['currency']}"
                f" ({_proof_types_label(types)})"
            )
            kb.append([InlineKeyboardButton(text=f"✅ {t['title'][:28]}", callback_data=f"mt:do:{t['id']}")])
        if _mgram_enabled(app):
            done = {r["task_id"] for r in app.db.all(
                "SELECT task_id FROM micro_mgram_done WHERE user_id=?", uid
            )}
            listing = await fetch_pending_result(app.cfg, _mgram_bot_id(app), uid)
            if listing.reason != "ok":
                logging.getLogger("child.mgram").info(
                    "mgram tasks skipped bot=%s user=%s reason=%s",
                    _mgram_bot_id(app), uid, listing.reason,
                )
                if is_owner(app, uid):
                    hint = _mgram_owner_hint(listing.reason)
                    if hint:
                        lines.append(hint)
            for task in listing.tasks:
                tid = task.get("id")
                if not tid or tid in done:
                    continue
                try:
                    reward = to_minor(task.get("reward") or 0)
                except ValueError:
                    continue
                btn_text = task.get("button_text") or task.get("title") or "Выполнить"
                lines.append(
                    f"• 📡 {html.escape(str(btn_text))} — {fmt(reward)} {app.s['currency']} (MGRAM)"
                )
                kb.append([InlineKeyboardButton(
                    text=f"📡 {str(btn_text)[:24]}",
                    callback_data=f"mt:mgram:{tid}",
                )])
        if not available and not kb:
            extra = ""
            if _mgram_enabled(app) and is_owner(app, uid) and len(lines) > 1:
                extra = "\n\n" + "\n".join(lines[1:])
            await message.answer("Сейчас нет доступных заданий (или достигнут лимит заявок)." + extra)
            return
        if not lines[1:]:
            lines.append("Нет доступных заданий.")
        await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None)

    @r.callback_query(F.data.startswith("mt:do:"))
    async def start_do(cb: CallbackQuery, state: FSMContext) -> None:
        tid = _callback_int(cb.data, prefix="mt:do:")
        if tid is None:
            await cb.answer("Задание недоступно", show_alert=True)
            return
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
        types = _proof_types(task)
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
        types = _proof_types(task)
        proof = _validate_proof(message, types)
        if not proof:
            await message.answer(f"Нужно доказательство одного из типов: {_proof_types_label(types)}")
            return
        uid = message.from_user.id
        status, reason, reward = _submit_proof(app, uid, tid, proof)
        await state.clear()
        if status is None:
            await message.answer(reason)
            return
        if status == "approved":
            await message.answer(f"Задание выполнено! Начислено {fmt(reward)} {app.s['currency']}.")
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

    @r.callback_query(F.data.startswith("mt:mgram:"))
    async def mgram_show(cb: CallbackQuery) -> None:
        if not _mgram_enabled(app):
            await cb.answer("Задания MGRAM выключены", show_alert=True)
            return
        try:
            tid = int(cb.data.split(":")[-1])
        except ValueError:
            await cb.answer("Неверная кнопка", show_alert=True)
            return
        listing = await fetch_pending_result(app.cfg, _mgram_bot_id(app), cb.from_user.id)
        if listing.reason != "ok":
            logging.getLogger("child.mgram").info(
                "mgram task view skipped bot=%s user=%s reason=%s",
                _mgram_bot_id(app), cb.from_user.id, listing.reason,
            )
        task = next((item for item in listing.tasks if item.get("id") == tid), None)
        if not task:
            await cb.answer("Задание недоступно или уже выполнено", show_alert=True)
            return
        try:
            reward = to_minor(task.get("reward") or 0)
        except ValueError:
            await cb.answer("Задание недоступно или уже выполнено", show_alert=True)
            return
        link = str(task.get("link") or "")
        text = (
            f"📡 <b>Задание MGRAM</b>\n\n"
            f"{html.escape(str(task.get('button_text') or task.get('title') or 'Выполнить'))}\n"
            f"Награда: {fmt(reward)} {app.s['currency']}\n\n"
            "Перейдите по ссылке, выполните действие, затем нажмите «Проверить»."
        )
        rows = []
        if link:
            rows.append([InlineKeyboardButton(text="🔗 Перейти", url=link)])
        rows.append([InlineKeyboardButton(text="✅ Проверить", callback_data=f"mt:mgramcheck:{tid}")])
        app.db.remember_mgram_shown(cb.from_user.id, tid, reward)
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                disable_web_page_preview=True)
        await cb.answer()

    @r.callback_query(F.data.startswith("mt:mgramcheck:"))
    async def mgram_check(cb: CallbackQuery) -> None:
        if not _mgram_enabled(app):
            await cb.answer("Задания MGRAM выключены", show_alert=True)
            return
        try:
            tid = int(cb.data.split(":")[-1])
        except ValueError:
            await cb.answer("Неверная кнопка", show_alert=True)
            return
        uid = cb.from_user.id
        already = app.db.one("SELECT reward FROM micro_mgram_done WHERE user_id=? AND task_id=?", uid, tid)
        if already:
            await cb.answer("Уже засчитано", show_alert=True)
            return
        result = await check_completion(app.cfg, _mgram_bot_id(app), uid, tid)
        if not result.confirmed:
            await cb.answer("Задание ещё не выполнено. Попробуйте снова после выполнения.", show_alert=True)
            return
        shown = app.db.one("SELECT reward FROM micro_mgram_shown WHERE user_id=? AND task_id=?", uid, tid)
        try:
            if result.reward is not None:
                reward = to_minor(result.reward)
            elif shown:
                reward = as_minor(shown["reward"])
            else:
                reward = None
        except ValueError:
            reward = None
        if reward is None:
            await cb.answer("Не удалось определить награду. Попробуйте позже.", show_alert=True)
            return
        credited = app.db.credit_mgram_task(uid, tid, reward)
        if credited is None:
            await cb.answer("Уже засчитано", show_alert=True)
            return
        logging.getLogger("child.mgram").info(
            "mgram task credited bot=%s user=%s task=%s reward=%s",
            _mgram_bot_id(app), uid, tid, credited,
        )
        await cb.answer(f"Задание выполнено! +{fmt(credited)}", show_alert=True)
        await cb.message.answer(f"Задание MGRAM засчитано. Начислено {fmt(credited)} {app.s['currency']}.")

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
        reward = to_minor(app.s.get("ref_reward") or 0)
        await message.answer(
            f"Приглашайте друзей по ссылке:\n<code>{link}</code>\n\n"
            f"Награда за друга: {fmt(reward)} {app.s['currency']}"
        )

    return r


def build_admin_router(app) -> Router:
    r = Router(name="microtasks-admin")
    from child.withdrawals import LiveOwnerFilter
    r.message.filter(LiveOwnerFilter(app))
    r.callback_query.filter(LiveOwnerFilter(app))

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
            await cb.answer("Нет доступа", show_alert=True)
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
            reward = to_minor((message.text or "").replace(",", "."))
            if reward <= 0:
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
        if not is_owner(app, cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        data = await state.get_data()
        if not {"title", "reward", "proof_types", "approval_mode", "repeat_mode"} <= set(data):
            await cb.answer("Черновик устарел — начните заново.", show_alert=True)
            return
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
        if not is_owner(app, cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        await state.clear()
        await cb.message.answer("Создание отменено.")
        await cb.answer()

    @r.callback_query(F.data.startswith("mt:toggle:"))
    async def toggle(cb: CallbackQuery) -> None:
        if not is_owner(app, cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        tid = _callback_int(cb.data, prefix="mt:toggle:")
        if tid is None:
            await cb.answer("Задание недоступно", show_alert=True)
            return
        app.db.exec("UPDATE micro_tasks SET is_active = 1 - is_active WHERE id=?", tid)
        await cb.message.edit_reply_markup(reply_markup=tasks_kb())
        await cb.answer()

    @r.callback_query(F.data.startswith("mt:admin:queue:"))
    async def queue(cb: CallbackQuery) -> None:
        if not is_owner(app, cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        # Авто-отклонение старых заявок (то же правило, что и у фоновой задачи)
        try:
            hours = int(app.s.get("auto_reject_hours") or 48)
        except (TypeError, ValueError):
            hours = 48
        app.db.reject_stale_micro_submissions(hours)
        page = _callback_int(cb.data, prefix="mt:admin:queue:")
        if page is None:
            await cb.answer("Очередь недоступна", show_alert=True)
            return
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
        if not is_owner(app, cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        sid = _callback_int(cb.data, prefix="mt:ok:")
        if sid is None:
            await cb.answer("Уже обработано", show_alert=True)
            return
        reward = app.db.approve_micro_submission(sid)
        if reward is None:
            await cb.answer("Уже обработано", show_alert=True)
            return
        await cb.message.edit_text((cb.message.text or "") + "\n\n✅ Одобрено")
        await cb.answer("Одобрено")

    @r.callback_query(F.data.startswith("mt:no:"))
    async def reject_start(cb: CallbackQuery, state: FSMContext) -> None:
        if not is_owner(app, cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        sid = _callback_int(cb.data, prefix="mt:no:")
        if sid is None:
            await cb.answer("Уже обработано", show_alert=True)
            return
        await state.update_data(reject_id=sid)
        await state.set_state("mt_reject_comment")
        await cb.message.answer("Комментарий к отклонению (или - чтобы без комментария):")
        await cb.answer()

    @r.message(StateFilter("mt_reject_comment"))
    async def reject_finish(message: Message, state: FSMContext) -> None:
        if not is_owner(app, message.from_user.id):
            await state.clear()
            await message.answer("Нет доступа")
            return
        data = await state.get_data()
        sid = data.get("reject_id")
        comment = "" if message.text == "-" else (message.text or "")[:500]
        ok = bool(sid) and app.db.reject_micro_submission(int(sid), comment)
        await state.clear()
        await message.answer("Заявка отклонена." if ok else "Уже обработано.")

    return r
