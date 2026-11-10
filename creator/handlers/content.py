"""Creator content navigation. Every operation rechecks private chat and current ownership."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from child.referral_club import ClubError
from child.star_journey import JourneyError
from creator import content_views as views
from creator.content import KINDS, ContentError, decode, open_content
from creator.keyboards import BTN_CONTENT
from creator.services import Ctx
from creator.states import ContentInput

router = Router(name="creator-content")
WRITING = {"new", "resume", "edit", "field", "correct", "input"}
MUTATIONS = WRITING | {"save", "publish", "pause", "activate", "copy", "discard", "prepare"}
ERRORS = (ContentError, ClubError, JourneyError)


def perform(c, action, args):
    if action == "home" and not args:
        return views.home(c)
    if action == "prepare" and not args:
        return views.home(c)
    if c.engine is None:
        raise ContentError("Сначала подготовьте каталог на главном экране контента.")
    if action == "admin" and len(args) == 1:
        return views.catalog(c, decode(args[0]))
    if action == "calendar" and len(args) == 1:
        return views.calendar(c, decode(args[0]))
    if action in {"manage", "preview"} and len(args) == 1:
        renderer = views.owner_card if action == "manage" else views.preview
        return renderer(c, decode(args[0]))
    if action in {"new", "resume"} and not args:
        if action == "resume" and not c.form_get():
            raise ContentError("Сохранённой формы нет. Нажмите «Создать».")
        c.form_begin()
        return views.form(c)
    if action == "edit" and len(args) == 2:
        c.form_begin(decode(args[0]), decode(args[1]))
        return views.form(c)
    if action == "field" and len(args) == 2:
        c.form_field(args[0], decode(args[1]))
        return views.form(c)
    if action == "correct" and len(args) == 2 and c.code == "r":
        form = c.form_get()
        if not form or form["step"] != "correct":
            raise ContentError("Откройте актуальный шаг формы.")
        c.form_input(decode(args[0]), decode(args[1]))
        return views.form(c)
    if action == "input" and len(args) == 3 and c.code == "s":
        form = c.form_get()
        if not form or args[0] not in {"category", "correct"} or form["step"] != args[0]:
            raise ContentError("Откройте актуальный шаг формы.")
        value = decode(args[1]) if args[0] == "correct" else args[1]
        c.form_input(value, decode(args[2]))
        return views.form(c)
    if action == "save" and len(args) == 1:
        return views.owner_card(c, c.form_save(decode(args[0])))
    if action in {"publish_view", "publish", "pause", "activate"} and len(args) == 2:
        cid, revision = map(decode, args)
        if action == "publish_view":
            value = c.owner_data(cid)
            if value["status"] != "draft" or value["revision"] != revision:
                raise ContentError("Материал уже изменился. Откройте каталог заново.")
            return views.owner_card(c, cid, confirm=True)
        if action == "publish":
            c.check_text({field: c.owner_data(cid)[field] for field in c.fields})
            c.engine.publish(c.uid, cid, revision)
        else:
            c.engine.set_status(c.uid, cid, "paused" if action == "pause" else "active", revision)
        return views.owner_card(c, cid)
    if action == "copy" and len(args) == 1:
        copier = c.engine.copy_draft if c.code == "r" else c.engine.copy_card
        return views.owner_card(c, copier(c.uid, decode(args[0])))
    if action in {"discard", "later"} and len(args) == 1:
        version = decode(args[0])
        if action == "discard":
            c.form_discard(version)
        else:
            c._form_version(version)
        return views.home(c)
    raise ContentError("Кнопка устарела. Откройте контент заново.")


async def deliver(cb, text, keyboard):
    try:
        await cb.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in exc.message.lower():
            return True
    except TelegramAPIError:
        pass
    await cb.answer("Экран не отправлен. Сохранённая форма доступна через «Продолжить».", show_alert=True)
    return False


def private(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    return isinstance(message, Message) and message.chat.type == "private" and message.chat.id == event.from_user.id


@router.message(F.text == BTN_CONTENT)
async def entry(message: Message, state: FSMContext, ctx: Ctx):
    if not private(message):
        await message.answer("Откройте контент в личном чате с конструктором.")
        return
    await state.clear()
    text, keyboard = views.bot_list(ctx, message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("content:"))
async def list_bots(cb: CallbackQuery, state: FSMContext, ctx: Ctx):
    if not private(cb):
        await cb.answer("Откройте контент в личном чате с конструктором.", show_alert=True)
        return
    try:
        parts = cb.data.split(":")
        if len(parts) != 3 or parts[:2] != ["content", "all"]:
            raise ContentError("Кнопка устарела.")
        before = await state.get_state(), await state.get_data()
        if before[0] not in {None, ContentInput.active.state}:
            raise ContentError("Завершите текущую настройку или нажмите /cancel.")
        screen = views.bot_list(ctx, cb.from_user.id, decode(parts[2]))
        if await deliver(cb, *screen):
            if (await state.get_state(), await state.get_data()) != before:
                raise ContentError("Откройте актуальный список контента.")
            await state.clear()
            await cb.answer()
    except ERRORS as exc:
        await cb.answer(str(exc), show_alert=True)


@router.callback_query(F.data.startswith("cnt:"))
async def action(cb: CallbackQuery, state: FSMContext, ctx: Ctx):
    if not private(cb):
        await cb.answer("Откройте контент в личном чате с конструктором.", show_alert=True)
        return
    try:
        parts = cb.data.split(":")
        if len(parts) < 4 or parts[2] not in KINDS:
            raise ContentError("Кнопка устарела.")
        bot_id, code, kind, args = decode(parts[1]), parts[2], parts[3], parts[4:]
        before = await state.get_state(), await state.get_data()
        if before[0] is not None and not (
            before[0] == ContentInput.active.state
            and before[1].get("bot_id") == bot_id
            and before[1].get("template") == KINDS[code]
        ):
            raise ContentError("Завершите текущую настройку или нажмите /cancel.")
        with open_content(
            ctx, cb.from_user.id, bot_id, code, write=kind in MUTATIONS, prepare=kind == "prepare" and not args
        ) as content:
            screen = perform(content, kind, args)
            draft = content.form_get() if kind in WRITING else None
            version = draft["version"] if draft else None
        if not await deliver(cb, *screen):
            return
        if (await state.get_state(), await state.get_data()) != before:
            await cb.answer("Откройте актуальный экран формы.", show_alert=True)
            return
        with open_content(ctx, cb.from_user.id, bot_id, code) as content:
            if draft and (not content.form_get() or content.form_get()["version"] != version):
                raise ContentError("Форма изменилась. Откройте её заново.")
        if draft:
            await state.set_state(ContentInput.active)
            await state.set_data({"bot_id": bot_id, "template": KINDS[code], "version": version})
        else:
            await state.clear()
        await cb.answer()
    except ERRORS as exc:
        await cb.answer(str(exc), show_alert=True)


@router.message(ContentInput.active, F.text)
async def text_input(message: Message, state: FSMContext, ctx: Ctx):
    if not private(message):
        return
    try:
        data = await state.get_data()
        code = next((key for key, value in KINDS.items() if value == data.get("template")), None)
        if code is None or type(data.get("bot_id")) is not int or type(data.get("version")) is not int:
            raise ContentError("Форма устарела. Откройте каталог заново.")
        with open_content(ctx, message.from_user.id, data["bot_id"], code, write=True) as content:
            form = content._form_version(data["version"])
            if form["step"] in {"category", "correct", "confirm"}:
                raise ContentError("Выберите действие кнопкой в форме.")
            content.form_input(message.text, data["version"])
            screen = views.form(content)
            version = content.form_get()["version"]
        await message.answer(screen[0], reply_markup=screen[1])
        if await state.get_data() == data:
            with open_content(ctx, message.from_user.id, data["bot_id"], code) as content:
                if content.form_get()["version"] != version:
                    raise ContentError("Форма изменилась. Нажмите «Продолжить».")
            await state.update_data(version=version)
    except ERRORS as exc:
        await message.answer(str(exc))
