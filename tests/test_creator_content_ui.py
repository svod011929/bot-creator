"""Content management through the Creator Dispatcher and fake Telegram, with real child databases."""

import html
import re
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update

from creator.app import build_dispatcher
from creator.content import callback, encode
from creator.content_views import bind
from creator.states import ContentInput, CreateBot
from tests.conftest import cb_update, msg_update
from tests.test_clicker_adventure_ui import callbacks, last_screen
from tests.test_creator_content import OTHER, OWNER, content_env, data, snapshot  # noqa: F401


@pytest.fixture
async def runtime(content_env, bot, session):  # noqa: F811
    w = content_env
    dp = build_dispatcher(w.ctx)
    w.state = dp.fsm.get_context(bot=bot, user_id=OWNER, chat_id=OWNER)

    async def feed(update):
        await dp.feed_update(bot, update)

    w.feed, w.dp, w.bot = feed, dp, bot
    w.cb = lambda action: callback(w.ctx.db.bot(w.id), action)
    yield w
    await dp.storage.close()


async def click(w, session, prefix):
    value = next(c for c in callbacks(last_screen(session)) if c.startswith(w.cb(prefix)))
    await w.feed(cb_update(OWNER, value))
    return value


async def fill(w, session):
    with w.open() as c:
        fields = c.fields
    values = {**data(w), "title": "Creator draft title"}
    for field in fields:
        if field == "category":
            await click(w, session, "input:category:" + values[field] + ":")
        elif field == "correct":
            prefix = "correct:" if w.code == "r" else "input:correct:"
            await click(w, session, prefix + encode(values[field]) + ":")
        else:
            value = "\n".join(values[field]) if field == "answers" else str(values[field])
            await w.feed(msg_update(OWNER, value))


async def test_create_preview_save_edit_publish_and_copy_from_creator(runtime, session):
    w = runtime
    await w.feed(msg_update(OWNER, "/start"))
    assert "📚 Контент ботов" in [b.text for row in session.of(SendMessage)[-1].reply_markup.keyboard for b in row]
    await w.feed(msg_update(OWNER, "📚 Контент ботов"))
    assert w.cb("home") in callbacks(last_screen(session))
    await w.feed(cb_update(OWNER, w.cb("home")))
    await click(w, session, "new")
    await fill(w, session)
    assert "Предпросмотр" in last_screen(session).text
    await click(w, session, "field:title:")
    await w.feed(msg_update(OWNER, "Revised in Creator"))
    save = await click(w, session, "save:")
    with w.open() as c:
        table = "ref_campaigns" if w.code == "r" else "star_cards"
        cid = c.db.scalar(f"SELECT MAX(id) FROM {table}")
        assert c.owner_data(cid)["status"] == "draft"
        assert c.owner_data(cid)["title"] == "Revised in Creator"
        before = snapshot(c.db)
    await click(w, session, "preview:")
    assert "Только предпросмотр" in last_screen(session).text
    with w.open() as c:
        assert snapshot(c.db) == before
    await click(w, session, "manage:")
    await click(w, session, "edit:")
    await click(w, session, "field:days:")
    await w.feed(msg_update(OWNER, "5"))
    await click(w, session, "save:")
    await click(w, session, "publish_view:")
    with w.open() as c:
        assert c.owner_data(cid)["status"] == "draft"
    publish = await click(w, session, "publish:")
    with w.open() as c:
        assert c.owner_data(cid)["status"] == "active"
        assert c.owner_data(cid)["days"] == 5
    for stale in (save, publish):
        await w.feed(cb_update(OWNER, stale))
        assert session.of(AnswerCallbackQuery)[-1].show_alert
    await w.feed(cb_update(OWNER, w.cb("manage:" + encode(cid))))
    await click(w, session, "pause:")
    await click(w, session, "activate:")
    await click(w, session, "copy:")
    with w.open() as c:
        copy = c.db.scalar(f"SELECT MAX(id) FROM {table}")
        assert c.owner_data(copy)["status"] == "draft" and copy != cid
    await w.feed(cb_update(OWNER, w.cb("calendar:0")))
    assert "Календарь контента" in last_screen(session).text
    assert "Revised in Creator" in last_screen(session).text
    assert w.app.db.user(OWNER)["balance"] == 17.5


async def test_form_survives_cancel_and_new_dispatcher(runtime, session):
    w = runtime
    await w.feed(cb_update(OWNER, w.cb("new")))
    if w.code == "s":
        await click(w, session, "input:category:logic:")
    await w.feed(msg_update(OWNER, "Saved between restarts"))
    with w.open() as c:
        stored = c.form_get()
    await w.feed(msg_update(OWNER, "/cancel"))
    assert await w.state.get_state() is None
    with w.open() as c:
        assert c.form_get() == stored
    await w.dp.storage.close()
    w.dp = build_dispatcher(w.ctx)
    w.state = w.dp.fsm.get_context(bot=w.bot, user_id=OWNER, chat_id=OWNER)

    async def feed(update):
        await w.dp.feed_update(w.bot, update)

    w.feed = feed
    await w.feed(cb_update(OWNER, w.cb("resume")))
    assert await w.state.get_state() == ContentInput.active.state
    await w.feed(msg_update(OWNER, "Continued content"))
    with w.open() as c:
        assert c.form_get()["data"]["title"] == "Saved between restarts"
        key = "description" if w.code == "r" else "lesson"
        assert c.form_get()["data"][key] == "Continued content"
    await click(w, session, "discard:")
    with w.open() as c:
        assert c.form_get() is None
    await w.dp.storage.close()


async def test_unrelated_fsm_is_preserved_until_user_opens_main_content(runtime, session):
    w = runtime
    await w.state.set_state(CreateBot.token)
    await w.state.set_data({"sentinel": "old form"})
    await w.feed(cb_update(OWNER, w.cb("new")))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert await w.state.get_state() == CreateBot.token.state
    with w.open() as c:
        assert c.form_get() is None
    await w.feed(msg_update(OWNER, "📚 Контент ботов"))
    assert await w.state.get_state() is None
    assert "Контент моих ботов" in last_screen(session).text


async def test_form_input_rechecks_owner_and_wrong_template(runtime, session):
    w = runtime
    await w.feed(cb_update(OWNER, w.cb("new")))
    if w.code == "s":
        await click(w, session, "input:category:logic:")
    with w.open() as c:
        before = c.form_get()
    w.ctx.db.transfer_bot(w.id, OTHER)
    await w.feed(msg_update(OWNER, "must not be written"))
    row = w.app.db.one("SELECT * FROM creator_content_forms WHERE owner_id=?", OWNER)
    assert row["version"] == before["version"]
    await w.feed(cb_update(OWNER, f"cnt:{encode(w.id)}:{'s' if w.code == 'r' else 'r'}:admin:0"))
    assert session.of(AnswerCallbackQuery)[-1].show_alert


@pytest.mark.parametrize("kind", ["group", "other_private", "missing", "inaccessible"])
async def test_content_callbacks_require_own_accessible_private_chat(runtime, session, kind):
    w = runtime
    raw = cb_update(OWNER, w.cb("home")).model_dump(exclude_none=True)
    q = raw["callback_query"]
    if kind == "group":
        q["message"]["chat"].update(type="group", id=-123)
    elif kind == "other_private":
        q["message"]["chat"]["id"] = OTHER
    elif kind == "missing":
        q.pop("message")
        q["inline_message_id"] = "synthetic"
    else:
        q["message"]["date"] = 0
        q["message"].pop("text", None)
    before = snapshot(w.app.db)
    session.clear()
    await w.feed(Update.model_validate(raw))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert not session.of(EditMessageText)
    assert snapshot(w.app.db) == before


@pytest.mark.parametrize("suffix", ["unknown", "new:extra", "admin:-1", "manage:00", "save:0", "publish:1:0", "field:missing:0"])
async def test_forged_buttons_do_not_mutate_catalog_or_forms(runtime, session, suffix):
    w = runtime
    before = snapshot(w.app.db)
    await w.feed(cb_update(OWNER, w.cb(suffix)))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert snapshot(w.app.db) == before


async def test_send_failure_preserves_form_for_resume(runtime, session, monkeypatch):
    w = runtime
    original = session.make_request

    async def fail(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: cannot edit")
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", fail)
    await w.feed(cb_update(OWNER, w.cb("new")))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert await w.state.get_state() is None
    with w.open() as c:
        assert c.form_get()
    monkeypatch.setattr(session, "make_request", original)
    await w.feed(cb_update(OWNER, w.cb("resume")))
    assert await w.state.get_state() == ContentInput.active.state


async def test_large_preview_and_creator_callback_limits(runtime, session):
    w = runtime
    values = {**data(w), "title": "🌟" * 80, "question": "🌟" * 180, "answers": ["🌟" * 80, "🌈" * 80, "🌍" * 80]}
    values["description" if w.code == "r" else "lesson"] = "🌟" * (900 if w.code == "r" else 700)
    cid = w.native.create_draft(OWNER, values)
    for action in ("manage:", "preview:"):
        await w.feed(cb_update(OWNER, w.cb(action + encode(cid))))
        screen = last_screen(session)
        visible = html.unescape(re.sub(r"</?(?:b|i|code)>", "", screen.text))
        assert len(visible.encode("utf-16-le")) // 2 <= 4096
        assert all(len(c.encode()) <= 64 for c in callbacks(screen))


def test_maximum_identifiers_fit_in_creator_callbacks():
    n = str(2**63 - 1)
    source = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="publish", callback_data=f"sky:publish_view:{n}:{n}")],
            [InlineKeyboardButton(text="input", callback_data=f"sky:input:category:workshop:{n}")],
            [InlineKeyboardButton(text="discard", callback_data="sky:discard")],
        ]
    )
    c = SimpleNamespace(
        code="s", row={"id": 2**63 - 1, "template": "starbot", "token": ""}, form_get=lambda: {"version": 2**63 - 1}
    )
    _, result = bind(c, "<b>preview</b>", source)
    assert all(len(value.encode()) <= 64 for value in callbacks(SimpleNamespace(reply_markup=result)))
