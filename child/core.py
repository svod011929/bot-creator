"""Общая логика всех дочерних ботов: старт, рефералы, обязательная подписка,
профиль, вывод средств и админка владельца."""
from __future__ import annotations

import asyncio
import html
import logging
import math
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

from child import templates
from child.db import ChildDB, fmt
from child.files import read_config, read_settings, write_setting
from child.piarflow import CHECK_CB as PF_CHECK_CB
from child.piarflow import PiarFlowGate
from child.subscription import INVITE_URL, PUBLIC_URL, USERNAME, GateResult, SubscriptionGate, parse_channel
from child.tgrass import CHECK_CB as TG_CHECK_CB
from child.tgrass import TEXT as TG_TEXT
from child.tgrass import TGrassGate

log = logging.getLogger("child")

BTN_PROFILE = "👤 Профиль"
BTN_WITHDRAW = "💸 Вывод"
BTN_ADMIN = "⚙️ Админка"


class ChildApp:
    def __init__(self, bot_dir: Path):
        self.dir = Path(bot_dir)
        self.cfg: dict[str, Any] = read_config(self.dir)
        self.template = templates.load(self.cfg["template"])
        self.info: templates.TemplateInfo = self.template.INFO
        self.db = ChildDB(self.dir / "bot.db")
        self.game = self.template.create_game(self.db) if hasattr(self.template, "create_game") else None
        self.club = self.template.create_club(self) if hasattr(self.template, "create_club") else None
        self.username: str = self.cfg.get("username") or ""
        self._settings: dict[str, Any] = {}
        self._mtime: float = -1
        self.subscription = SubscriptionGate(self.dir, self.owner_id)
        key = str(self.cfg.get("tgrass_key") or "")
        self.tgrass: TGrassGate | None = (
            TGrassGate(key, self.db, str(self.cfg.get("tgrass_base_url") or "https://tgrass.space")) if key else None
        )
        pf_key = str(self.cfg.get("piarflow_key") or "")
        self.piarflow: PiarFlowGate | None = (
            PiarFlowGate(pf_key, self.db, str(self.cfg.get("piarflow_base_url") or "https://piarflow.com/v1"))
            if pf_key else None
        )

    async def tgrass_gate(self, user, force: bool = False) -> InlineKeyboardMarkup | None:
        """Спонсорские задания (TGrass, затем PiarFlow): None — пропускаем, иначе клавиатура со спонсорами."""
        from child import sponsor_log as slog
        if self.is_owner(user.id):
            slog.log_skip(log, "gate", "owner")
            return None
        if self.tgrass is None and self.piarflow is None:
            slog.log_skip(log, "gate", "no_key")
            return None
        for name, gate in (("TGrass", self.tgrass), ("PiarFlow", self.piarflow)):
            if gate is None:
                continue
            try:
                kb = await gate.check(user, force=force)
            except Exception:  # noqa: BLE001 — без тела ответа/токенов в сообщении
                log.exception("%s gate failed", name)
                kb = None
            if kb is not None:
                return kb
        return None

    async def close_gates(self) -> None:
        for gate in (self.tgrass, self.piarflow):
            if gate is not None:
                await gate.close()

    @property
    def owner_id(self) -> int:
        return int(self.cfg["owner_id"])

    def is_owner(self, uid: int) -> bool:
        return uid == self.owner_id

    @property
    def s(self) -> dict[str, Any]:
        """Настройки с автоперечитыванием settings.json при изменении."""
        path = self.dir / "settings.json"
        stat = path.stat() if path.exists() else None
        mtime = (stat.st_ino, stat.st_mtime_ns, stat.st_size) if stat else None
        if mtime != self._mtime:
            self._settings = read_settings(self.dir, self.info.defaults())
            self._mtime = mtime
        return self._settings

    def set(self, key: str, value: Any) -> None:
        write_setting(self.dir, key, value)
        self._mtime = -1

    @property
    def currency(self) -> str:
        return str(self.s.get("currency", ""))

    def ref_link(self, uid: int) -> str:
        return f"https://t.me/{self.username}?start=r{uid}"

    def footer(self) -> str:
        if self.cfg.get("vip"):
            return ""
        creator = self.cfg.get("creator_username")
        return f"\n\n<i>🤖 Бот создан в @{creator}</i>" if creator else ""

    def menu(self, uid: int) -> ReplyKeyboardMarkup:
        rows = [list(r) for r in self.template.menu_rows(self)]
        rows.append([BTN_PROFILE, BTN_WITHDRAW])
        if self.is_owner(uid):
            rows.append([BTN_ADMIN])
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True
        )

    # ---------- обязательная подписка ----------
    async def gate_result(self, bot: Bot, uid: int) -> GateResult:
        return await self.subscription.check(bot, uid)

    def gate_keyboard(self) -> InlineKeyboardMarkup:
        channel = str(self.s.get("required_channel") or "")
        url = str(self.s.get("channel_url") or "")
        if not (PUBLIC_URL.fullmatch(url) or INVITE_URL.fullmatch(url)):
            url = ""
        if not url and channel.startswith("@"):
            try:
                name = parse_channel(channel)
                url = f"https://t.me/{name[1:]}"
            except ValueError:
                pass
        rows = []
        if url:
            rows.append([InlineKeyboardButton(text="📢 Подписаться", url=url)])
        rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="chk_sub")])
        return InlineKeyboardMarkup(inline_keyboard=rows)


class GateMiddleware(BaseMiddleware):
    """Не пускает к функциям бота без подписки на обязательный канал."""

    def __init__(self, app: ChildApp):
        self.app = app

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        bot: Bot = data["bot"]
        if user is None:
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data in ("chk_sub", TG_CHECK_CB, PF_CHECK_CB):
            return await handler(event, data)
        tg_kb = await self.app.tgrass_gate(user)
        if tg_kb is not None:
            if isinstance(event, CallbackQuery):
                await event.answer("Сначала подпишитесь на спонсоров", show_alert=True)
                if event.message:
                    await event.message.answer(TG_TEXT, reply_markup=tg_kb)
            elif isinstance(event, Message):
                await event.answer(TG_TEXT, reply_markup=tg_kb)
            return None
        result = await self.app.gate_result(bot, user.id)
        if result.allowed:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer(result.text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(result.text, reply_markup=self.app.gate_keyboard())
        return None


class Withdraw(StatesGroup):
    amount = State()
    details = State()


class AdminStates(StatesGroup):
    broadcast = State()
    setting = State()


async def safe_send(bot: Bot, chat_id: int, text: str, **kw: Any) -> bool:
    try:
        await bot.send_message(chat_id, text, **kw)
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await safe_send(bot, chat_id, text, **kw)
    except Exception as e:  # noqa: BLE001
        log.info("send to %s failed: %s", chat_id, e)
        return False


async def send_profile(message: Message, app: ChildApp, uid: int) -> None:
    from child.withdrawals import history_entry

    u = app.db.user(uid)
    if not u:
        await message.answer("Нажмите /start")
        return
    text = (
        f"👤 <b>Профиль</b>\n\n🆔 ID: <code>{u['id']}</code>\n"
        f"💰 Баланс: <b>{fmt(u['balance'])} {app.currency}</b>\n"
        f"👥 Приглашено: {app.db.referrals(u['id'])}\n\n"
        f"🔗 Ваша ссылка:\n{app.ref_link(u['id'])}"
    )
    if app.info.key == "starbot":
        text += ("\n\n<i>Это внутренний баланс бота, а не баланс Telegram Stars. "
                 "Условия реальных наград и ручных выплат уточняйте у владельца бота.</i>")
    if hasattr(app.template, "profile_extra"):
        text += app.template.profile_extra(app, uid)
    await message.answer(text, disable_web_page_preview=True, reply_markup=history_entry())


def build_core_router(app: ChildApp) -> Router:
    r = Router(name="core")

    async def after_gate(message: Message, uid: int) -> None:
        """Пользователь прошёл проверку подписки: награда пригласившему и меню."""
        reward = float(app.s.get("ref_reward", 0) or 0)
        ref_id = app.db.reward_referrer_once(uid, reward)
        if ref_id and reward > 0:
            await safe_send(message.bot, ref_id,
                            f"🎉 По вашей ссылке пришёл новый пользователь! +{fmt(reward)} {app.currency}")
        if hasattr(app.template, "on_access_confirmed"):
            app.template.on_access_confirmed(app, uid)
        text = html.escape(str(app.s.get("welcome", "Добро пожаловать!"))) + app.footer()
        await message.answer(text, reply_markup=app.menu(uid))

    async def enter_after_check(message: Message, uid: int, result: GateResult) -> None:
        if not result.allowed:
            await message.answer(result.text, reply_markup=app.gate_keyboard())
        elif not result.confirmed:
            await message.answer(result.text, reply_markup=app.menu(uid))
        else:
            await after_gate(message, uid)

    @r.message(CommandStart())
    async def start(message: Message, command: CommandObject, state: FSMContext) -> None:
        await state.clear()
        u = message.from_user
        if message.chat.type == "private" and hasattr(app.template, "pause_forms"):
            app.template.pause_forms(app, u.id)
        if hasattr(app.template, "start_user"):
            app.template.start_user(app, u, command.args or "", private=message.chat.type == "private")
        else:
            ref = None
            if command.args and command.args.startswith("r") and command.args[1:].isdigit():
                ref = int(command.args[1:])
            app.db.upsert_user(u.id, u.username, u.full_name, ref)
        tg_kb = await app.tgrass_gate(u)
        if tg_kb is not None:
            await message.answer(TG_TEXT, reply_markup=tg_kb)
            return
        result = await app.gate_result(message.bot, u.id)
        await enter_after_check(message, u.id, result)

    @r.callback_query(F.data == "chk_sub")
    async def check_sub(cb: CallbackQuery) -> None:
        result = await app.gate_result(cb.bot, cb.from_user.id)
        if not result.confirmed:
            await cb.answer(result.text, show_alert=True)
            if result.allowed and cb.message:
                await enter_after_check(cb.message, cb.from_user.id, result)
            return
        await cb.answer("✅ Спасибо!")
        u = cb.from_user
        app.db.upsert_user(u.id, u.username, u.full_name, None)
        await cb.message.delete()
        await after_gate(cb.message, u.id)

    @r.callback_query(F.data.in_({TG_CHECK_CB, PF_CHECK_CB}))
    async def check_tgrass(cb: CallbackQuery) -> None:
        u = cb.from_user
        tg_kb = await app.tgrass_gate(u, force=True)
        if tg_kb is not None:
            await cb.answer("❌ Вы подписались не на всех спонсоров", show_alert=True)
            try:
                await cb.message.edit_reply_markup(reply_markup=tg_kb)
            except TelegramBadRequest:
                pass
            return
        await cb.answer("✅ Спасибо!")
        app.db.upsert_user(u.id, u.username, u.full_name, None)
        try:
            await cb.message.delete()
        except TelegramBadRequest:
            pass
        result = await app.gate_result(cb.bot, u.id)
        await enter_after_check(cb.message, u.id, result)

    @r.message(F.text == BTN_PROFILE)
    async def profile(message: Message) -> None:
        if message.chat.type == "private" and hasattr(app.template, "pause_forms"):
            app.template.pause_forms(app, message.from_user.id)
        await send_profile(message, app, message.from_user.id)

    # ---------- вывод ----------
    @r.message(F.text == BTN_WITHDRAW)
    async def withdraw(message: Message, state: FSMContext) -> None:
        if message.chat.type == "private" and hasattr(app.template, "pause_forms"):
            app.template.pause_forms(app, message.from_user.id)
        u = app.db.user(message.from_user.id)
        if not u:
            return
        min_w = float(app.s.get("min_withdraw", 0) or 0)
        if u["balance"] < min_w or u["balance"] <= 0:
            await message.answer(
                f"💸 Минимальная сумма вывода: {fmt(min_w)} {app.currency}\nВаш баланс: {fmt(u['balance'])} {app.currency}"
            )
            return
        await state.set_state(Withdraw.amount)
        await message.answer(
            f"💸 Введите сумму вывода (от {fmt(min_w)} до {fmt(u['balance'])} {app.currency}).\n/cancel — отмена"
        )

    @r.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.chat.type == "private" and hasattr(app.template, "cancel_form"):
            app.template.cancel_form(app, message.from_user.id)
        await message.answer("Отменено.", reply_markup=app.menu(message.from_user.id))

    @r.message(Withdraw.amount, ~F.text.in_(getattr(app.template, "STATE_ESCAPE_TEXTS", ())))
    async def withdraw_amount(message: Message, state: FSMContext) -> None:
        try:
            amount = float((message.text or "").replace(",", "."))
        except ValueError:
            await message.answer("Введите число.")
            return
        u = app.db.user(message.from_user.id)
        min_w = float(app.s.get("min_withdraw", 0) or 0)
        if not math.isfinite(amount) or amount <= 0 or amount < min_w or amount > u["balance"]:
            await message.answer(f"Сумма должна быть от {fmt(min_w)} до {fmt(u['balance'])} {app.currency}.")
            return
        await state.update_data(amount=amount)
        await state.set_state(Withdraw.details)
        await message.answer(str(app.s.get("withdraw_details_prompt", "Укажите реквизиты для выплаты:")))

    @r.message(Withdraw.details, ~F.text.in_(getattr(app.template, "STATE_ESCAPE_TEXTS", ())))
    async def withdraw_details(message: Message, state: FSMContext) -> None:
        from child.withdrawals import format_unit

        data = await state.get_data()
        await state.clear()
        details = (message.text or "").strip()[:300]
        currency = app.currency
        wid = app.db.create_withdrawal(message.from_user.id, data["amount"], details, currency=currency)
        if wid is None:
            await message.answer("Недостаточно средств.")
            return
        await message.answer(f"✅ Заявка №{wid} отправлена владельцу бота. Выплату владелец проводит вручную; "
                             "статус заявки — в «👤 Профиль» → «📜 Мои заявки».", reply_markup=app.menu(message.from_user.id))
        u = message.from_user
        await safe_send(
            message.bot, app.owner_id,
            f"💸 <b>Заявка на вывод №{wid}</b>\nОт: {html.escape(u.full_name)} (<code>{u.id}</code>"
            f"{', @' + u.username if u.username else ''})\nСумма: {fmt(data['amount'])} {format_unit(currency)}\n"
            f"Реквизиты: <code>{html.escape(details)}</code>",
            reply_markup=wd_kb(wid),
        )

    return r


def wd_kb(wid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отметить выплаченной", callback_data=f"wd:ok:{wid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd:no:{wid}"),
    ]])


def subscription_link(app: ChildApp) -> InlineKeyboardMarkup | None:
    username = str(app.cfg.get("creator_username") or "")
    bot_id = app.cfg.get("bot_id")
    if not USERNAME.fullmatch(username) or not isinstance(bot_id, int) or bot_id <= 0:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🔒 Настроить подписку в конструкторе", url=f"https://t.me/{username}?start=sub_{bot_id}"
    )]])


async def subscription_redirect(message: Message, app: ChildApp):
    markup = subscription_link(app)
    text = ("🔒 Обязательная подписка настраивается мастером в конструкторе." if markup
            else "Переход к мастеру недоступен: откройте своего бота в конструкторе.")
    await message.answer(text, reply_markup=markup)


def admin_kb(app: ChildApp) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
         InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:bc")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm:set"),
         InlineKeyboardButton(text="💸 Заявки", callback_data="adm:wd")],
    ]
    link = subscription_link(app)
    if link:
        rows.extend(link.inline_keyboard)
    for text, data in getattr(app.template, "ADMIN_BUTTONS", []):
        rows.append([InlineKeyboardButton(text=text, callback_data=data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_admin_router(app: ChildApp) -> Router:
    r = Router(name="owner-admin")
    r.message.filter(F.from_user.id == app.owner_id)
    r.callback_query.filter(F.from_user.id == app.owner_id)

    @r.message(Command("admin"))
    @r.message(F.text == BTN_ADMIN)
    async def admin(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.chat.type == "private" and hasattr(app.template, "pause_forms"):
            app.template.pause_forms(app, message.from_user.id)
        await message.answer("⚙️ <b>Панель владельца</b>", reply_markup=admin_kb(app))

    @r.callback_query(F.data == "adm:stats")
    async def stats(cb: CallbackQuery) -> None:
        from child.withdrawals import format_unit

        s = app.db.stats()
        paid = app.db.paid_by_unit()
        lines = [f"• {fmt(total)} {format_unit(unit)}" for unit, _, total in paid]
        await cb.message.answer(
            f"📊 <b>Статистика</b>\n\n👥 Пользователей: {s['users']}\n"
            f"🆕 Сегодня (с 00:00 по времени сервера): {s['today']}\n"
            f"🚫 Недоступны для рассылки: {s['blocked']}\n⏳ Заявок на вывод: {s['pending']}\n"
            f"✅ Отмечено выплаченными: {sum(n for _, n, _ in paid)}"
            + ("\n" + "\n".join(lines) if lines else "")
            + "\n\n<i>Суммы — по единице, сохранённой в каждой заявке; это ручные отметки, не подтверждение перевода.</i>"
            + (app.template.admin_stats_extra(app) if hasattr(app.template, "admin_stats_extra") else "")
        )
        await cb.answer()

    @r.callback_query(F.data == "adm:bc")
    async def bc_start(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.broadcast)
        await cb.message.answer("📢 Пришлите сообщение для рассылки (текст, фото, видео). /cancel — отмена")
        await cb.answer()

    @r.message(AdminStates.broadcast, F.text != "/cancel")
    async def bc_send(message: Message, state: FSMContext) -> None:
        await state.clear()
        ids = app.db.active_ids()
        await message.answer(f"🚀 Рассылка запущена на {len(ids)} пользователей.")

        async def run() -> None:
            ok = fail = 0
            for uid in ids:
                try:
                    await message.copy_to(uid)
                    ok += 1
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except TelegramForbiddenError:
                    app.db.mark_blocked(uid)
                    fail += 1
                except Exception:  # noqa: BLE001
                    fail += 1
                await asyncio.sleep(0.05)
            await safe_send(message.bot, app.owner_id, f"✅ Рассылка завершена: доставлено {ok}, ошибок {fail}.")

        asyncio.create_task(run())

    @r.callback_query(F.data == "adm:set")
    async def settings(cb: CallbackQuery) -> None:
        lines = ["⚙️ <b>Настройки</b>\n"]
        rows = []
        for st in app.info.settings:
            if st.key in {"required_channel", "channel_url"}:
                continue
            lines.append(f"• {st.label}: <code>{html.escape(st.show(app.s.get(st.key)))}</code>")
            rows.append([InlineKeyboardButton(text=st.label, callback_data=f"set:{st.key}")])
        link = subscription_link(app)
        if link:
            rows.extend(link.inline_keyboard)
        else:
            lines.append("Переход к мастеру подписки недоступен; откройте бота в конструкторе.")
        await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await cb.answer()

    @r.callback_query(F.data.startswith("set:"))
    async def setting_pick(cb: CallbackQuery, state: FSMContext) -> None:
        key = cb.data.split(":", 1)[1]
        if key in {"required_channel", "channel_url"}:
            await state.clear()
            await subscription_redirect(cb.message, app)
            await cb.answer()
            return
        st = app.info.setting(key)
        if not st:
            await cb.answer()
            return
        await state.set_state(AdminStates.setting)
        await state.update_data(key=key)
        hint = " (@канал или ID; «-» чтобы убрать)" if st.kind == "channel" else ""
        await cb.message.answer(f"Введите новое значение для «{st.label}»{hint}. /cancel — отмена")
        await cb.answer()

    @r.message(AdminStates.setting, F.text != "/cancel")
    async def setting_save(message: Message, state: FSMContext) -> None:
        key = (await state.get_data()).get("key")
        if key in {"required_channel", "channel_url"}:
            await state.clear()
            await subscription_redirect(message, app)
            return
        st = app.info.setting(key or "")
        try:
            value = st.parse(message.text or "")
        except (ValueError, AttributeError):
            await message.answer("❌ Неверное значение, попробуйте ещё раз.")
            return
        await state.clear()
        app.set(st.key, value)
        await message.answer(f"✅ «{st.label}» сохранено.")

    return r
