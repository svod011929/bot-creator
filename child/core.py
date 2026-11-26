"""Общая логика всех дочерних ботов: старт, рефералы, обязательная подписка,
профиль, вывод средств и админка владельца."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.methods.base import TelegramMethod
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

from child import templates
from child.boostly import BTN_TASKS as BTN_BOOSTLY
from child.boostly import CHECK_CB as BL_CHECK_CB
from child.boostly import TASKS_CHECK_CB as BL_TASKS_CHECK_CB
from child.boostly import TASKS_REFRESH_CB as BL_TASKS_REFRESH_CB
from child.boostly import BoostlyGate
from child.db import ChildDB, fmt
from child.engagement import Engagement, private_observation
from child.files import read_config, read_settings, write_setting
from child.money import to_minor
from child.piarflow import CHECK_CB as PF_CHECK_CB
from child.piarflow import PiarFlowGate
from child.subscription import INVITE_URL, PUBLIC_URL, USERNAME, GateResult, SubscriptionGate, parse_channel
from child.tgrass import CHECK_CB as TG_CHECK_CB
from child.tgrass import TEXT as TG_TEXT
from child.tgrass import TGrassGate

log = logging.getLogger("child")

USER_BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="profile", description="Профиль"),
    BotCommand(command="withdraw", description="Вывод"),
]
ADMIN_BOT_COMMAND = BotCommand(command="admin", description="Админка")

# Conservative defaults via Bot API 10.5 setMySettings. Privacy mode stays on.
DEFAULT_BOT_SETTINGS = {
    "can_join_groups": True,
    "can_read_all_group_messages": False,
    "can_communicate_with_bots": False,
    "has_topics_enabled": False,
    "has_topic_sidebar": False,
    "callback_query_mode": "default",
}


class SetMySettings(TelegramMethod[bool]):
    """Change bot settings that used to live only in BotFather (Bot API 10.5)."""

    __returning__ = bool
    __api_method__ = "setMySettings"

    can_join_groups: bool | None = None
    can_read_all_group_messages: bool | None = None
    can_communicate_with_bots: bool | None = None
    has_topics_enabled: bool | None = None
    has_topic_sidebar: bool | None = None
    callback_query_mode: str | None = None


async def apply_bot_settings(bot: Bot) -> None:
    """Apply DEFAULT_BOT_SETTINGS. Ignore servers that do not support the method yet."""
    try:
        await bot(SetMySettings(**DEFAULT_BOT_SETTINGS))
    except Exception:  # noqa: BLE001
        # Do not log the exception: HTTP clients may include the bot token in the URL.
        log.debug("setMySettings недоступен")


async def install_bot_commands(bot: Bot, app: "ChildApp") -> None:
    """Default menu for everyone; /admin only for the owner chat."""
    try:
        await bot.set_my_commands(list(USER_BOT_COMMANDS))
    except Exception:  # noqa: BLE001
        log.warning("Не удалось установить меню команд")
        return
    if not app.owner_id:
        return
    try:
        await bot.set_my_commands(
            [*USER_BOT_COMMANDS, ADMIN_BOT_COMMAND],
            scope=BotCommandScopeChat(chat_id=app.owner_id),
        )
    except Exception:  # noqa: BLE001
        log.warning("Не удалось установить меню команд для владельца")


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

        def analytics_owner(uid):
            current = read_config(self.dir)
            if not isinstance(current, dict) or type(current.get("owner_id")) is not int:
                raise ValueError("Owner unavailable")
            return current["owner_id"] == uid

        self.engagement = Engagement(self.db, owner_check=analytics_owner)
        self.game = self.template.create_game(self.db) if hasattr(self.template, "create_game") else None
        self.club = self.template.create_club(self) if hasattr(self.template, "create_club") else None
        self.sky = self.template.create_journey(self) if hasattr(self.template, "create_journey") else None
        self.events = self.template.create_events(self) if hasattr(self.template, "create_events") else None
        self.teams = self.template.create_teams(self) if hasattr(self.template, "create_teams") else None
        self.tracks = self.template.create_tracks(self) if hasattr(self.template, "create_tracks") else None
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
        bl_sock = str(self.cfg.get("boostly_proxy") or "").strip()
        bl_token = str(self.cfg.get("boostly_proxy_token") or "").strip()
        has_bl_proxy = bool(bl_sock and bl_token)
        self.boostly: BoostlyGate | None = (
            BoostlyGate(self.cfg, self.db, int(self.cfg.get("bot_id") or 0)) if has_bl_proxy else None
        )

    async def tgrass_gate(self, user, force: bool = False) -> InlineKeyboardMarkup | None:
        """Спонсорские задания (TGrass, затем PiarFlow): None — пропускаем, иначе клавиатура со спонсорами."""
        from child import sponsor_log as slog
        if self.is_owner(user.id):
            slog.log_skip(log, "gate", "owner")
            return None
        if self.tgrass is None and self.piarflow is None and self.boostly is None:
            slog.log_skip(log, "gate", "no_key")
            return None
        for name, gate in (("TGrass", self.tgrass), ("PiarFlow", self.piarflow), ("Boostly", self.boostly)):
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
        for gate in (self.tgrass, self.piarflow, self.boostly):
            if gate is not None:
                await gate.close()

    @property
    def owner_id(self) -> int:
        """Current owner from config.json, not the value cached at process start."""
        from child.files import read_config
        try:
            cfg = read_config(self.dir)
            owner = cfg.get("owner_id") if isinstance(cfg, dict) else None
            if type(owner) is int:
                self.cfg["owner_id"] = owner
                return owner
        except (OSError, ValueError, TypeError):
            pass
        return int(self.cfg["owner_id"])

    def is_owner(self, uid: int) -> bool:
        from child.withdrawals import is_owner as live_owner
        return live_owner(self, uid)

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
        if self.boostly is not None:
            rows.append([BTN_BOOSTLY])
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
        # Owner moderation ban (distinct from blocked). Owner is always allowed.
        if user.id != self.app.owner_id and self.app.db.is_banned(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ ограничен владельцем бота", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Доступ ограничен владельцем бота.")
            return None
        # /start registers the user and shows sponsors itself.
        # Every other message and button still goes through the sponsor check below.
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data in ("chk_sub", TG_CHECK_CB, PF_CHECK_CB, BL_CHECK_CB):
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
            message = event.message if isinstance(event, CallbackQuery) else event
            if isinstance(message, Message) and not user.is_bot:
                private_observation(self.app, message, user.id, "interaction")
                if result.confirmed:
                    private_observation(self.app, message, user.id, "access")
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
    user_lookup = State()


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
        reward = to_minor(app.s.get("ref_reward", 0) or 0)
        ref_id = app.db.reward_referrer_once(uid, reward)
        if ref_id and reward > 0:
            await safe_send(message.bot, ref_id,
                            f"🎉 По вашей ссылке пришёл новый пользователь! +{fmt(reward)} {app.currency}")
        if hasattr(app.template, "on_access_confirmed"):
            app.template.on_access_confirmed(app, uid)
        private_observation(app, message, uid, "access")
        text = html.escape(str(app.s.get("welcome", "Добро пожаловать!"))) + app.footer()
        await message.answer(text, reply_markup=app.menu(uid))
        if message.chat.type == "private" and message.chat.id == uid:
            from child.presentation import runtime_guide

            launch = runtime_guide(app)
            if launch:
                await message.answer(launch[0], reply_markup=launch[1])

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
            fresh = app.template.start_user(app, u, command.args or "", private=message.chat.type == "private")
        else:
            ref = None
            if command.args and command.args.startswith("r") and command.args[1:].isdigit():
                ref = int(command.args[1:])
            fresh = app.db.upsert_user(u.id, u.username, u.full_name, ref)
        if not u.is_bot:
            private_observation(app, message, u.id, "start", registered=fresh)
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

    @r.callback_query(F.data.in_({TG_CHECK_CB, PF_CHECK_CB, BL_CHECK_CB}))
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

    @r.message(F.text == BTN_BOOSTLY)
    async def boostly_tasks(message: Message) -> None:
        if app.boostly is None:
            return
        text, kb = await app.boostly.render_list(message.from_user)
        await message.answer(text, reply_markup=kb, disable_web_page_preview=True)

    @r.callback_query(F.data.in_({BL_TASKS_CHECK_CB, BL_TASKS_REFRESH_CB}))
    async def boostly_tasks_action(cb: CallbackQuery) -> None:
        if app.boostly is None:
            await cb.answer()
            return
        checking = cb.data == BL_TASKS_CHECK_CB
        text, kb = await app.boostly.render_list(cb.from_user, checking=checking)
        if checking:
            await cb.answer("✅ Проверено" if "пока нет" not in text else "Заданий пока нет")
        else:
            await cb.answer()
        try:
            await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest:
            pass

    @r.message(Command("profile"))
    @r.message(F.text == BTN_PROFILE)
    async def profile(message: Message) -> None:
        if message.chat.type == "private" and hasattr(app.template, "pause_forms"):
            app.template.pause_forms(app, message.from_user.id)
        await send_profile(message, app, message.from_user.id)

    # ---------- вывод ----------
    @r.message(Command("withdraw"))
    @r.message(F.text == BTN_WITHDRAW)
    async def withdraw(message: Message, state: FSMContext) -> None:
        if message.chat.type == "private" and hasattr(app.template, "pause_forms"):
            app.template.pause_forms(app, message.from_user.id)
        u = app.db.user(message.from_user.id)
        if not u:
            return
        min_w = to_minor(app.s.get("min_withdraw", 0) or 0)
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
            amount = to_minor((message.text or "").replace(",", "."))
        except ValueError:
            await message.answer("Введите число.")
            return
        u = app.db.user(message.from_user.id)
        min_w = to_minor(app.s.get("min_withdraw", 0) or 0)
        if amount <= 0 or amount < min_w or amount > u["balance"]:
            await message.answer(f"Сумма должна быть от {fmt(min_w)} до {fmt(u['balance'])} {app.currency}.")
            return
        from child.payout_flow import start_method_choice
        if await start_method_choice(message, state, app, amount):
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
    # Группы: обзор · люди · управление · инструменты конструктора
    rows = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
         InlineKeyboardButton(text="👤 Пользователи", callback_data="adm:users:0")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm:set"),
         InlineKeyboardButton(text="💸 Заявки", callback_data="adm:wd")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:bc"),
         InlineKeyboardButton(text="📜 Журнал", callback_data="adm:olog:0")],
    ]
    link = subscription_link(app)
    if link:
        rows.extend(link.inline_keyboard)
    for text, data in getattr(app.template, "ADMIN_BUTTONS", []):
        rows.append([InlineKeyboardButton(text=text, callback_data=data)])
    creator = str(app.cfg.get("creator_username") or "")
    bot_id = app.cfg.get("bot_id")
    if creator and bot_id:
        tpl = app.info.key if hasattr(app.info, "key") else ""
        tool_row = []
        if tpl == "clicker":
            tool_row.append(InlineKeyboardButton(text="🎯 События", url=f"https://t.me/{creator}?start=events_{bot_id}"))
        elif tpl == "refbot":
            tool_row.append(InlineKeyboardButton(text="🤝 Команды", url=f"https://t.me/{creator}?start=teams_{bot_id}"))
        elif tpl == "starbot":
            tool_row.append(InlineKeyboardButton(text="📚 Треки", url=f"https://t.me/{creator}?start=tracks_{bot_id}"))
        tool_row.append(InlineKeyboardButton(text="🎨 Оформление", url=f"https://t.me/{creator}?start=pres_{bot_id}"))
        if tool_row:
            rows.append(tool_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _user_card_text(app, u) -> str:
    created = time.strftime("%Y-%m-%d", time.localtime(u["created_at"])) if u["created_at"] else "—"
    pending = app.db.scalar(
        "SELECT COUNT(*) FROM withdrawals WHERE user_id=? AND status='pending'", u["id"]
    ) or 0
    return (
        f"👤 <b>{html.escape(u['full_name'] or '')}</b> "
        f"{('@' + html.escape(u['username'])) if u['username'] else ''}\n"
        f"ID: <code>{u['id']}</code>\nБаланс: {fmt(u['balance'])}\n"
        f"Регистрация: {created}\n"
        f"blocked: {'да' if u['blocked'] else 'нет'}\n"
        f"Заявок на вывод (ожидают): {pending}\n"
        f"Статус: {'🚫 забанен' if u['banned'] else '✅ активен'}"
    )

def _user_card_kb(u) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разбанить" if u["banned"] else "🚫 Забанить",
                              callback_data=f"adm:ban:{u['id']}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:users:0")],
    ])

def build_admin_router(app: ChildApp) -> Router:
    r = Router(name="owner-admin")
    from child.withdrawals import LiveOwnerFilter
    r.message.filter(LiveOwnerFilter(app))
    r.callback_query.filter(LiveOwnerFilter(app))
    r.message.filter(F.chat.type == "private")
    r.callback_query.filter(F.message.chat.type == "private")

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
        text = (
            f"📊 <b>Статистика</b>\n\n👥 Пользователей: {s['users']}\n"
            f"🆕 Сегодня (с 00:00 по времени сервера): {s['today']}\n"
            f"🚫 Недоступны для рассылки: {s['blocked']}\n⏳ Заявок на вывод: {s['pending']}\n"
            f"✅ Отмечено выплаченными: {sum(n for _, n, _ in paid)}"
            + ("\n" + "\n".join(lines) if lines else "")
            + "\n\n<i>Суммы — по единице, сохранённой в каждой заявке; это ручные отметки, не подтверждение перевода.</i>"
            + (app.template.admin_stats_extra(app) if hasattr(app.template, "admin_stats_extra") else "")
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:main")]])
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()

    @r.callback_query(F.data == "adm:main")
    async def admin_main(cb: CallbackQuery) -> None:
        await cb.message.edit_text("⚙️ <b>Панель владельца</b>", reply_markup=admin_kb(app))
        await cb.answer()

    @r.callback_query(F.data.startswith("adm:users:"))
    async def users_list(cb: CallbackQuery) -> None:
        parts = cb.data.split(":")
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        flt = parts[3] if len(parts) > 3 and parts[3] in {"all", "banned", "blocked"} else "all"
        PAGE = 8
        where = ""
        if flt == "banned":
            where = "WHERE banned=1"
        elif flt == "blocked":
            where = "WHERE blocked=1"
        total = app.db.scalar(f"SELECT COUNT(*) FROM users {where}") or 0
        pages = max(1, -(-total // PAGE))
        page = max(0, min(page, pages - 1))
        rows = app.db.all(
            f"SELECT id, username, full_name, balance, banned, blocked, created_at FROM users {where} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            PAGE, page * PAGE,
        )
        label = {"all": "все", "banned": "забаненные", "blocked": "blocked"}[flt]
        lines = [f"👤 <b>Пользователи</b> · {label} · стр. {page + 1}/{pages} · всего {total}"]
        kb_rows = []
        for r in rows:
            mark = "🚫" if r["banned"] else ("⛔" if r["blocked"] else "✅")
            name = (r["username"] and f"@{r['username']}") or (r["full_name"] or str(r["id"]))
            kb_rows.append([InlineKeyboardButton(text=f"{mark} {name[:22]}", callback_data=f"adm:u:{r['id']}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm:users:{page - 1}:{flt}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm:users:{page + 1}:{flt}"))
        if nav:
            kb_rows.append(nav)
        kb_rows.append([
            InlineKeyboardButton(text="все", callback_data="adm:users:0:all"),
            InlineKeyboardButton(text="бан", callback_data="adm:users:0:banned"),
            InlineKeyboardButton(text="blocked", callback_data="adm:users:0:blocked"),
        ])
        kb_rows.append([InlineKeyboardButton(text="🔎 Поиск", callback_data="adm:usearch")])
        kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:main")])
        await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await cb.answer()

    @r.callback_query(F.data == "adm:usearch")
    async def user_search_start(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.user_lookup)
        await cb.message.edit_text("🔎 Пришлите ID или @username:", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:users:0")]]))
        await cb.answer()

    @r.message(AdminStates.user_lookup, F.text)
    async def user_search(message: Message, state: FSMContext) -> None:
        raw = message.text.strip().lstrip("@")
        rows = app.db.user_list(limit=5, search=raw)
        if not rows:
            await message.answer("Не найдено. Попробуйте ещё или /cancel.")
            return
        await state.clear()
        if len(rows) == 1:
            u = rows[0]
            await message.answer(_user_card_text(app, u), reply_markup=_user_card_kb(u))
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"ID {r['id']}", callback_data=f"adm:u:{r['id']}")] for r in rows
        ] + [[InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:users:0")]])
        await message.answer("Найдено несколько:", reply_markup=kb)

    @r.callback_query(F.data.startswith("adm:u:"))
    async def user_open(cb: CallbackQuery) -> None:
        raw = cb.data.split(":")[-1]
        if not raw.isdigit():
            await cb.answer("Кнопка устарела", show_alert=True)
            return
        u = app.db.one("SELECT * FROM users WHERE id=?", int(raw))
        if not u:
            await cb.answer("Не найден", show_alert=True)
            return
        await cb.message.edit_text(_user_card_text(app, u), reply_markup=_user_card_kb(u))
        await cb.answer()

    @r.callback_query(F.data.startswith("adm:ban:"))
    async def user_ban(cb: CallbackQuery) -> None:
        raw = cb.data.split(":")[-1]
        if not raw.isdigit():
            await cb.answer("Кнопка устарела", show_alert=True)
            return
        uid = int(raw)
        if uid == app.owner_id:
            await cb.answer("Нельзя забанить владельца", show_alert=True)
            return
        u = app.db.one("SELECT banned FROM users WHERE id=?", uid)
        if not u:
            await cb.answer("Не найден", show_alert=True)
            return
        new_banned = not bool(u["banned"])
        app.db.set_banned(uid, new_banned)
        app.db.log_owner(app.owner_id, "ban" if new_banned else "unban",
                         target=str(uid), detail="banned" if new_banned else "unbanned")
        u = app.db.one("SELECT * FROM users WHERE id=?", uid)
        await cb.message.edit_text(_user_card_text(app, u), reply_markup=_user_card_kb(u))
        await cb.answer()


    @r.callback_query(F.data.startswith("adm:olog:"))
    async def owner_log(cb: CallbackQuery) -> None:
        parts = cb.data.split(":")
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        PAGE = 8
        total = app.db.owner_actions_count()
        pages = max(1, -(-total // PAGE))
        page = max(0, min(page, pages - 1))
        rows = app.db.owner_actions(limit=PAGE, offset=page * PAGE)
        lines = [f"📜 <b>Журнал действий</b> · стр. {page + 1}/{pages} · всего {total}"]
        for r in rows:
            when = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
            detail = html.escape((r['detail'] or '')[:40])
            lines.append(f"{when} · {html.escape(r['action'])} · {html.escape(r['target'] or '')} · {detail}")
        if not rows:
            lines.append("Пусто")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm:olog:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm:olog:{page + 1}"))
        kb = InlineKeyboardMarkup(inline_keyboard=(
            ([nav] if nav else []) + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:main")]]
        ))
        await cb.message.edit_text("\n".join(lines), reply_markup=kb)
        await cb.answer()

    @r.callback_query(F.data == "adm:bc")
    async def bc_start(cb: CallbackQuery, state: FSMContext) -> None:
        if hasattr(app.template, "pause_forms"):
            app.template.pause_forms(app, cb.from_user.id)
        await state.set_state(AdminStates.broadcast)
        await cb.message.answer("📢 Пришлите сообщение для рассылки (текст, фото, видео). /cancel — отмена")
        await cb.answer()

    @r.message(AdminStates.broadcast, F.text != "/cancel", ~F.text.in_(getattr(app.template, "STATE_ESCAPE_TEXTS", ())))
    async def bc_preview(message: Message, state: FSMContext) -> None:
        ids = app.db.active_ids()
        await state.update_data(bc_chat=message.chat.id, bc_msg=message.message_id, bc_n=len(ids))
        await message.answer(
            f"👁 Предпросмотр. Отправить {len(ids)} пользователям?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Отправить {len(ids)}", callback_data="adm:bcgo")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:main")],
            ]),
        )

    @r.callback_query(F.data == "adm:bcgo")
    async def bc_send(cb: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        chat_id = data.get("bc_chat")
        msg_id = data.get("bc_msg")
        if not chat_id or not msg_id:
            await cb.answer("Сначала пришлите сообщение", show_alert=True)
            return
        ids = app.db.active_ids()
        await cb.message.edit_text(f"🚀 Рассылка запущена на {len(ids)} пользователей.")
        await cb.answer()

        async def run() -> None:
            ok = fail = 0
            for uid in ids:
                try:
                    await cb.bot.copy_message(uid, chat_id, msg_id)
                    ok += 1
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except TelegramForbiddenError:
                    app.db.mark_blocked(uid)
                    fail += 1
                except Exception:  # noqa: BLE001
                    fail += 1
                await asyncio.sleep(0.05)
            await safe_send(cb.bot, app.owner_id, f"✅ Рассылка завершена: доставлено {ok}, ошибок {fail}.")

        asyncio.create_task(run())

    @r.callback_query(F.data == "adm:set")
    @r.callback_query(F.data.startswith("adm:set:"))
    async def settings(cb: CallbackQuery) -> None:
        parts = cb.data.split(":")
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        items = [st for st in app.info.settings if st.key not in {"required_channel", "channel_url"}]
        PAGE = 6
        total = len(items)
        pages = max(1, -(-total // PAGE))
        page = max(0, min(page, pages - 1))
        slice_ = items[page * PAGE:(page + 1) * PAGE]
        lines = [f"⚙️ <b>Настройки</b> · стр. {page + 1}/{pages}"]
        rows = []
        for st in slice_:
            lines.append(f"• {st.label}: <code>{html.escape(st.show(app.s.get(st.key)))}</code>")
            rows.append([InlineKeyboardButton(text=st.label, callback_data=f"set:{st.key}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm:set:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm:set:{page + 1}"))
        if nav:
            rows.append(nav)
        link = subscription_link(app)
        if link and page == 0:
            rows.extend(link.inline_keyboard)
        elif page == 0 and not link:
            lines.append("Переход к мастеру подписки недоступен; откройте бота в конструкторе.")
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:main")])
        await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await cb.answer()

    @r.callback_query(F.data.startswith("set:"))
    async def setting_pick(cb: CallbackQuery, state: FSMContext) -> None:
        if hasattr(app.template, "pause_forms"):
            app.template.pause_forms(app, cb.from_user.id)
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

    @r.message(AdminStates.setting, F.text != "/cancel", ~F.text.in_(getattr(app.template, "STATE_ESCAPE_TEXTS", ())))
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
        try:
            app.db.log_owner(app.owner_id, "setting", target=st.key, detail="updated")
        except Exception:  # noqa: BLE001
            pass
        await message.answer(
            f"✅ «{st.label}» сохранено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К настройкам", callback_data="adm:set")],
                [InlineKeyboardButton(text="⬅️ В панель", callback_data="adm:main")],
            ]),
        )

    return r


STALE_CLAIMS_EVERY = 600


async def stale_claims_loop(app: ChildApp, every: float = STALE_CLAIMS_EVERY) -> None:
    """Periodically apply the same auto-reject rule as the owner queue."""
    if app.info.key != "microtasks":
        return
    log = logging.getLogger("child.microtasks")
    while True:
        try:
            try:
                hours = int(app.s.get("auto_reject_hours") or 48)
            except (TypeError, ValueError):
                hours = 48
            n = app.db.reject_stale_micro_submissions(hours)
            if n:
                log.info("auto-rejected stale submissions count=%s", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auto-reject failed")
        await asyncio.sleep(every)


def start_maintenance_tasks(app: ChildApp, *, every: float = STALE_CLAIMS_EVERY) -> list[asyncio.Task]:
    tasks: list[asyncio.Task] = []
    if app.info.key == "microtasks":
        tasks.append(asyncio.create_task(stale_claims_loop(app, every=every), name="stale-micro-submissions"))
    app._maintenance_tasks = tasks
    return tasks


async def stop_maintenance_tasks(app: ChildApp) -> None:
    tasks = getattr(app, "_maintenance_tasks", [])
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    app._maintenance_tasks = []
