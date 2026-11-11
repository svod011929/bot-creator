"""Private, current-owner presentation editing. All game preview buttons remain local."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from child.presentation import PRESETS, guide
from creator import presentation_views as views
from creator.content import ContentError, decode
from creator.presentation import PresentationError, open_presentation
from creator.states import PresentationInput

router = Router(name="presentation")
ERRORS = (PresentationError, ContentError)


def private(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    return (
        bool(event.from_user)
        and isinstance(message, Message)
        and message.chat.type == "private"
        and message.chat.id == event.from_user.id
    )


def perform(studio, action, args):
    if action == "home" and not args:
        return views.home(studio)
    if action == "presets" and not args:
        return views.presets(studio)
    if action == "current" and len(args) == 1:
        return views.preview(studio, args[0])
    if action == "preview" and len(args) == 2:
        return views.preview(studio, args[0], decode(args[1]))
    if action == "choose" and len(args) == 2:
        studio.begin(args[0], decode(args[1]))
        return views.home(studio)
    if action == "field" and len(args) == 2:
        return views.field(studio, args[0], decode(args[1]))
    if action == "toggle" and len(args) == 2 and args[0] in {"0", "1"}:
        version = decode(args[1])
        old = studio._version(version)["values"]["launch_scene"]
        selected = old.removeprefix("off:") or next(iter(PRESETS[studio.template]))
        studio.edit("launch_scene", "off:" + selected if args[0] == "0" else selected, version)
        return views.home(studio)
    if action in {"confirm", "refresh_view"} and len(args) == 1:
        return views.confirm(studio, decode(args[0]), refresh=action == "refresh_view")
    if action in {"apply", "refresh", "discard"} and len(args) == 1:
        getattr(studio, action)(decode(args[0]))
        return views.home(studio)
    raise PresentationError("Кнопка устарела. Откройте оформление заново.")


async def deliver(cb, screen):
    try:
        await cb.message.edit_text(screen[0], reply_markup=screen[1], disable_web_page_preview=True)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in exc.message.lower():
            return True
    except TelegramAPIError:
        pass
    await cb.answer("Экран не отправлен. Откройте оформление снова: черновик сохранён.", show_alert=True)
    return False


@router.callback_query(F.data.startswith("brand:"))
async def action(cb: CallbackQuery, state: FSMContext, ctx):
    if not private(cb):
        await cb.answer("Откройте оформление в личном чате с креатором.", show_alert=True)
        return
    try:
        parts = cb.data.split(":")
        if len(parts) < 3:
            raise PresentationError("Кнопка устарела.")
        bid, kind, args = decode(parts[1]), parts[2], parts[3:]
        before = await state.get_state(), await state.get_data()
        if before[0] is not None and not (before[0] == PresentationInput.value.state and before[1].get("bot_id") == bid):
            raise PresentationError("Завершите текущую настройку или нажмите /cancel.")
        note = None
        with open_presentation(ctx, cb.from_user.id, bid) as studio:
            if kind == "hint" and len(args) == 2:
                d = studio._version(decode(args[1]))
                screen = guide(studio.template, d["values"])
                index = decode(args[0])
                if not screen or not 0 <= index < len(screen[1].inline_keyboard):
                    raise PresentationError("Откройте предпросмотр первого шага заново.")
                note = "В дочернем боте: " + screen[1].inline_keyboard[index][0].text
            else:
                screen = perform(studio, kind, args)
            draft = studio.draft()
            identity = studio.identity
        if note:
            await cb.answer(note, show_alert=True)
            return
        if not await deliver(cb, screen):
            return
        if (await state.get_state(), await state.get_data()) != before:
            raise PresentationError("Откройте актуальный экран оформления.")
        with open_presentation(ctx, cb.from_user.id, bid) as studio:
            current = studio.draft()
            if studio.identity != identity or (current["version"] if current else None) != (draft["version"] if draft else None):
                raise PresentationError("Оформление изменилось. Откройте экран заново.")
        if kind == "field":
            await state.set_state(PresentationInput.value)
            await state.set_data({"bot_id": bid, "field": args[0], "version": decode(args[1])})
        else:
            await state.clear()
        await cb.answer()
    except ERRORS as exc:
        await cb.answer(str(exc), show_alert=True)


@router.message(PresentationInput.value, F.text)
async def text_input(message: Message, state: FSMContext, ctx):
    if not private(message):
        return
    try:
        data = await state.get_data()
        if type(data.get("bot_id")) is not int or type(data.get("version")) is not int or not isinstance(data.get("field"), str):
            raise PresentationError("Форма устарела. Откройте оформление заново.")
        value = "" if data["field"] == "launch_text" and message.text.strip() == "-" else message.text
        with open_presentation(ctx, message.from_user.id, data["bot_id"]) as studio:
            studio.edit(data["field"], value, data["version"])
            screen = views.home(studio)
        await message.answer(screen[0], reply_markup=screen[1])
        if await state.get_data() == data:
            await state.clear()
    except ERRORS as exc:
        await message.answer(str(exc))
