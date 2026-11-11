"""Creator presentation flows and opt-in native first-step cards with fake Telegram."""

import html
import re

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Update

from child.__main__ import build_dispatcher as child_dispatcher
from child.core import ChildApp
from child.files import write_setting
from child.presentation import PRESETS, TITLES, guide
from creator.app import build_dispatcher
from creator.content import encode
from creator.presentation import callback
from creator.states import Cabinet
from tests.conftest import cb_update, msg_update
from tests.test_clicker_adventure_ui import callbacks, last_screen
from tests.test_presentation import OTHER, OWNER, env  # noqa: F401

PLAYER = 300


@pytest.fixture
async def runtime(env, bot, session):  # noqa: F811
    w = env
    w.dp = build_dispatcher(w.ctx)
    w.bot = bot
    w.state = w.dp.fsm.get_context(bot=bot, user_id=OWNER, chat_id=OWNER)
    w.cb = lambda action: callback(w.id, action)

    async def feed(update):
        await w.dp.feed_update(bot, update)

    w.feed = feed
    yield w
    await w.dp.storage.close()


async def press(w, session, prefix):
    data = next(c for c in callbacks(last_screen(session)) if c.startswith(w.cb(prefix)))
    await w.feed(cb_update(OWNER, data))
    return data


async def begin(w, session):
    await w.feed(cb_update(OWNER, w.cb("home")))
    await press(w, session, "presets")
    await press(w, session, "choose:" + w.preset + ":")


async def test_card_entry_edit_preview_confirm_apply_and_safe_repeated_apply(runtime, session):
    w = runtime
    await w.feed(cb_update(OWNER, f"b:{w.id}:open"))
    assert w.cb("home") in callbacks(last_screen(session))
    original = (w.path / "settings.json").read_bytes()
    await begin(w, session)
    await press(w, session, "field:welcome:")
    await w.feed(msg_update(OWNER, "Привет, <b>команда</b> & исследователи!"))
    assert (w.path / "settings.json").read_bytes() == original
    await press(w, session, "preview:greeting:")
    assert "&lt;b&gt;команда&lt;/b&gt; &amp;" in last_screen(session).text
    await w.feed(cb_update(OWNER, w.cb("home")))
    await press(w, session, "preview:guide:")
    assert "brand:" in callbacks(last_screen(session))[0]
    await press(w, session, "hint:0:")
    assert "В дочернем боте" in session.of(AnswerCallbackQuery)[-1].text
    assert not (w.path / "bot.db").exists()
    await w.feed(cb_update(OWNER, w.cb("home")))
    await press(w, session, "confirm:")
    assert "Будут заменены" in last_screen(session).text
    apply = await press(w, session, "apply:")
    with w.open() as s:
        assert s.raw_settings()["welcome"] == "Привет, <b>команда</b> & исследователи!"
        assert s.status(s.draft(), s.raw_settings()) == "applied"
    stamp = (w.path / "settings.json").stat().st_mtime_ns
    await w.feed(cb_update(OWNER, apply))
    assert (w.path / "settings.json").stat().st_mtime_ns == stamp
    assert "уже применён" in last_screen(session).text


async def test_conflict_preview_refresh_requires_new_confirmation(runtime, session):
    w = runtime
    await begin(w, session)
    write_setting(w.path, "welcome", "Changed outside draft")
    await w.feed(cb_update(OWNER, w.cb("home")))
    assert "изменились" in last_screen(session).text
    await press(w, session, "refresh_view:")
    await press(w, session, "refresh:")
    assert "сохранённый черновик" in last_screen(session).text
    with w.open() as s:
        assert s.raw_settings()["welcome"] == "Changed outside draft"
    await press(w, session, "confirm:")
    await press(w, session, "apply:")
    with w.open() as s:
        assert s.raw_settings()["welcome"] != "Changed outside draft"


async def test_cancel_and_restart_keep_draft_but_unrelated_fsm_is_preserved(runtime, session):
    w = runtime
    await begin(w, session)
    await press(w, session, "field:welcome:")
    await w.feed(msg_update(OWNER, "/cancel"))
    with w.open() as s:
        saved = s.draft()
    await w.dp.storage.close()
    w.dp = build_dispatcher(w.ctx)
    w.state = w.dp.fsm.get_context(bot=w.bot, user_id=OWNER, chat_id=OWNER)
    await w.feed(cb_update(OWNER, w.cb("home")))
    with w.open() as s:
        assert s.draft() == saved
    await w.state.set_state(Cabinet.lave_amount)
    await w.state.set_data({"sentinel": "keep"})
    await w.feed(cb_update(OWNER, w.cb("presets")))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert await w.state.get_state() == Cabinet.lave_amount.state
    assert await w.state.get_data() == {"sentinel": "keep"}


async def test_invalid_input_and_owner_transfer_cannot_write_stale_field(runtime, session):
    w = runtime
    await begin(w, session)
    await press(w, session, "field:welcome:")
    with w.open() as s:
        old = s.draft()
    await w.feed(msg_update(OWNER, "x" * 1201))
    with w.open() as s:
        assert s.draft() == old
    w.ctx.db.transfer_bot(w.id, OTHER)
    await w.feed(msg_update(OWNER, "must not save"))
    row = w.ctx.db.one("SELECT version FROM presentation_drafts WHERE owner_id=? AND bot_id=?", OWNER, w.id)
    assert row[0] == old["version"]


async def test_toggle_guide_and_reset_to_scenario_text(runtime, session):
    w = runtime
    await begin(w, session)
    await press(w, session, "toggle:0:")
    await press(w, session, "preview:guide:")
    assert "выключена" in last_screen(session).text
    await w.feed(cb_update(OWNER, w.cb("home")))
    await press(w, session, "toggle:1:")
    await press(w, session, "field:launch_text:")
    await w.feed(msg_update(OWNER, "-"))
    await press(w, session, "preview:guide:")
    assert "Первые шаги" in last_screen(session).text
    with w.open() as s:
        assert s.draft()["values"]["launch_text"] == ""


async def test_disabling_then_enabling_preserves_selected_second_scenario(runtime, session):
    w = runtime
    key = list(PRESETS[w.template])[1]
    with w.open() as s:
        s.begin(key, 0)
    await w.feed(cb_update(OWNER, w.cb("home")))
    await press(w, session, "toggle:0:")
    await press(w, session, "toggle:1:")
    with w.open() as s:
        assert s.draft()["values"]["launch_scene"] == key


@pytest.mark.parametrize("kind", ["group", "other_private", "missing", "inaccessible"])
async def test_callbacks_require_own_accessible_private_chat(runtime, session, kind):
    w = runtime
    raw = cb_update(OWNER, w.cb("presets")).model_dump(exclude_none=True)
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
    session.clear()
    await w.feed(Update.model_validate(raw))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert not session.of(EditMessageText)
    assert not w.ctx.db.one("SELECT 1 FROM sqlite_master WHERE name='presentation_drafts'")


@pytest.mark.parametrize(
    "suffix", ["unknown", "choose:foreign:0", "field:currency:0", "preview:secret:0", "apply:0", "toggle:2:0"]
)
async def test_forged_buttons_do_not_create_or_mutate_draft(runtime, session, suffix):
    w = runtime
    before = (w.path / "settings.json").read_bytes()
    await w.feed(cb_update(OWNER, w.cb(suffix)))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert (w.path / "settings.json").read_bytes() == before
    assert not w.ctx.db.one("SELECT 1 FROM sqlite_master WHERE name='presentation_drafts'")


async def test_maximum_unicode_previews_fit_telegram_and_escape_content(runtime, session):
    w = runtime
    with w.open() as s:
        d = s.begin(w.preset, 0)
        for key, value in (("welcome", "🌟" * 1200), (TITLES[w.template], "🌟" * 80), ("launch_text", "🌟" * 700)):
            d = s.edit(key, value, d["version"])
    await w.feed(cb_update(OWNER, w.cb("home")))
    for tab in ("greeting", "section", "guide"):
        await w.feed(cb_update(OWNER, w.cb(f"preview:{tab}:{encode(d['version'])}")))
        screen = last_screen(session)
        text = html.unescape(re.sub(r"</?(?:b|i)>", "", screen.text))
        assert len(text.encode("utf-16-le")) // 2 <= 4096
        assert all(len(data.encode()) <= 64 for data in callbacks(screen))


async def test_delivery_failure_preserves_draft_and_apply_receipt(runtime, session, monkeypatch):
    w = runtime
    await w.feed(cb_update(OWNER, w.cb("presets")))
    choose = next(c for c in callbacks(last_screen(session)) if c.startswith(w.cb("choose:" + w.preset + ":")))
    original = session.make_request

    async def fail(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: cannot edit")
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", fail)
    await w.feed(cb_update(OWNER, choose))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    with w.open() as s:
        assert s.draft() is not None
        version = s.draft()["version"]
    await w.feed(cb_update(OWNER, w.cb("apply:" + encode(version))))
    with w.open() as s:
        assert s.status(s.draft(), s.raw_settings()) == "applied"
    monkeypatch.setattr(session, "make_request", original)
    await w.feed(cb_update(OWNER, w.cb("home")))
    assert "уже применён" in last_screen(session).text


@pytest.mark.parametrize("choice", [0, 1])
async def test_each_preset_launches_real_child_guide_and_native_action(runtime, session, choice):
    w = runtime
    key = list(PRESETS[w.template])[choice]
    with w.open() as s:
        d = s.begin(key, 0)
        s.apply(d["version"])
        expected = guide(w.template, d["values"])
    app = ChildApp(w.path)
    dp = child_dispatcher(app)
    session.clear()
    await dp.feed_update(w.bot, msg_update(PLAYER, "/start"))
    messages = session.of(SendMessage)
    assert len(messages) == 2
    assert messages[-1].text == expected[0]
    assert callbacks(messages[-1]) == callbacks(type("Screen", (), {"reply_markup": expected[1]})())
    assert app.db.user(PLAYER)["balance"] == 0
    await dp.feed_update(w.bot, cb_update(PLAYER, callbacks(messages[-1])[0]))
    assert not session.of(AnswerCallbackQuery)[-1].show_alert
    await dp.storage.close()
    app.db.conn.close()


async def test_no_opt_in_keeps_original_start_and_required_channel_still_gates_guide(runtime, session):
    w = runtime
    app = ChildApp(w.path)
    dp = child_dispatcher(app)
    session.clear()
    await dp.feed_update(w.bot, msg_update(PLAYER, "/start"))
    assert len(session.of(SendMessage)) == 1
    with w.open() as s:
        d = s.begin(w.preset, 0)
        s.apply(d["version"])
    write_setting(w.path, "required_channel", "@channel")
    session.member_status = "left"
    session.clear()
    await dp.feed_update(w.bot, msg_update(PLAYER, "/start"))
    assert not any("Первые шаги" in m.text for m in session.of(SendMessage))
    session.member_status = "member"
    await dp.feed_update(w.bot, cb_update(PLAYER, "chk_sub"))
    assert "Первые шаги" in session.of(SendMessage)[-1].text
    await dp.storage.close()
    app.db.conn.close()


async def test_group_start_never_receives_private_launch_card(runtime, session):
    w = runtime
    with w.open() as s:
        d = s.begin(w.preset, 0)
        s.apply(d["version"])
    app = ChildApp(w.path)
    dp = child_dispatcher(app)
    session.clear()
    await dp.feed_update(w.bot, msg_update(PLAYER, "/start", chat={"id": -123, "type": "group"}))
    assert not any("Первые шаги" in m.text for m in session.of(SendMessage))
    await dp.storage.close()
    app.db.conn.close()


def test_largest_field_callback_fits_telegram_limit():
    maximum = 2**63 - 1
    assert len(callback(maximum, f"field:adventure_intro:{encode(maximum)}").encode()) <= 64
