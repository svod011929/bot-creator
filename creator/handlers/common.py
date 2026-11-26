"""Старт, главное меню, общие middleware."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from creator.keyboards import BTN_ADMIN, MENU_BUTTONS, main_kb
from creator.services import Ctx

router = Router(name="common")

WELCOME = (
    "<b>👋 Добро пожаловать в конструктор ботов!</b>\n\n"
    "Создавайте и управляйте Telegram-ботами из готовых шаблонов — без программирования. "
    "Мы хостим бота, вы настраиваете контент, оформление и выплаты.\n\n"
    "🚀 <b>Начните здесь:</b>\n"
    "• «🔨 Создать бота» — выберите шаблон и пришлите токен от @BotFather\n"
    "• «🤖 Мои боты» — управление уже созданными\n"
    "• «💼 Мой кабинет» — баланс, пополнение, история\n\n"
    "❓ Нужна помощь? Отправьте /help или откройте «📰 Информация» → «❓ Помощь».\n\n"
    "Кабинет, партнёры и перепродажа — кнопками меню ниже."
)


class UserMiddleware(BaseMiddleware):
    """Регистрирует пользователя, блокирует забаненных, сбрасывает состояние по кнопкам меню."""

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        ctx: Ctx = data["ctx"]
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        is_start = isinstance(event, Message) and (event.text or "").startswith("/start")
        row = ctx.db.user(user.id)
        if row is None and not is_start:
            ctx.db.upsert_user(user.id, user.username, user.full_name)
            row = ctx.db.user(user.id)
        if row is not None and row["banned"] and not ctx.is_admin(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ ограничен", show_alert=True)
            return None
        if isinstance(event, Message) and (event.text in MENU_BUTTONS
                                           or (event.text == BTN_ADMIN and ctx.is_admin(user.id))):
            state: FSMContext | None = data.get("state")
            if state is not None:
                ctx.discard_pending_secrets(user.id)
                await state.clear()
                data["raw_state"] = None
        return await handler(event, data)


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    ctx.bot_lists.pop(message.from_user.id, None)
    ctx.discard_pending_secrets(message.from_user.id)
    u = message.from_user
    if command.args and command.args.startswith("sub_"):
        from creator.handlers.subscription import screen
        try:
            bot_id = int(command.args.removeprefix("sub_"))
        except ValueError:
            await message.answer("Бот не найден")
            return
        ctx.db.upsert_user(u.id, u.username, u.full_name, None)
        await screen(message, state, ctx, bot_id)
        return
    if command.args and command.args.startswith("lpayout_"):
        from creator.handlers.owner_payouts import open_from_start
        try:
            bot_id_s, wid_s = command.args.removeprefix("lpayout_").split("_", 1)
            bot_id, wid = int(bot_id_s), int(wid_s)
        except ValueError:
            await message.answer("Заявка не найдена")
            return
        ctx.db.upsert_user(u.id, u.username, u.full_name, None)
        await open_from_start(message, ctx, bot_id, wid)
        return
    for prefix, label in (("events_", "События"), ("teams_", "Команды"),
                          ("tracks_", "Треки"), ("pres_", "Оформление")):
        if command.args and command.args.startswith(prefix):
            try:
                bot_id = int(command.args.removeprefix(prefix))
            except ValueError:
                await message.answer("Бот не найден")
                return
            ctx.db.upsert_user(u.id, u.username, u.full_name, None)
            b = ctx.db.bot(bot_id)
            if not b or b["owner_id"] != u.id:
                await message.answer("Бот не найден или вы не владелец.")
                return
            await message.answer(
                f"🔗 {label} бота ID {bot_id}. Откройте карточку бота в «Мои боты» для управления.",
                reply_markup=main_kb(ctx.is_admin(u.id)),
            )
            return
    ref = None
    if command.args and command.args.startswith("ref") and command.args[3:].isdigit():
        ref = int(command.args[3:])
    is_new = ctx.db.upsert_user(u.id, u.username, u.full_name, ref)
    await message.answer(WELCOME, reply_markup=main_kb(ctx.is_admin(u.id)))
    if is_new and ref and ctx.db.user(u.id)["referrer_id"] == ref:
        try:
            await message.bot.send_message(ref, "👥 По вашей партнёрской ссылке зарегистрировался новый пользователь!")
        except Exception:  # noqa: BLE001
            pass


@router.callback_query(F.data == "cancel")
async def cancel_cb(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    ctx.bot_lists.pop(cb.from_user.id, None)
    ctx.discard_pending_secrets(cb.from_user.id)
    await cb.message.edit_text("❌ Отменено. Можете начать заново или открыть меню.")
    await cb.answer()


@router.message(F.text == "/cancel")
async def cancel_cmd(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    ctx.bot_lists.pop(message.from_user.id, None)
    ctx.discard_pending_secrets(message.from_user.id)
    await message.answer("❌ Отменено. Можете начать заново или открыть меню.",
                      reply_markup=main_kb(ctx.is_admin(message.from_user.id)))


@router.message(F.text == "/help")
async def help_cmd(message: Message, ctx: Ctx) -> None:
    from creator.handlers.info import help_text
    from creator.ui import show
    await show(message, help_text(ctx), main_kb(ctx.is_admin(message.from_user.id)))
