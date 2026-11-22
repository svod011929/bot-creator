"""Fake Telegram sessions exercise player events and the Creator's clicker content editor."""

import html
import re
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update

from child.clicker_events import FIELDS, SEED, EventError
from child.clicker_events_views import player_view
from child.clicker_game import GAME_TZ
from child.core import ChildApp
from child.files import read_config, write_config
from creator import content_views as views
from creator.app import build_dispatcher
from creator.config import Config
from creator.content import ContentError, callback, encode, open_content
from creator.db import DB
from creator.services import Ctx
from creator.supervisor import Supervisor
from tests.conftest import cb_update, msg_update
from tests.test_child_bots import OWNER, A, B, make
from tests.test_clicker_adventure_ui import callbacks, last_screen
from tests.test_clicker_events import snapshot


async def setup(tmp_path, bot):
    app, feed = make(tmp_path, "clicker", bot)
    clock = [int(datetime(2026, 11, 11, 12, tzinfo=GAME_TZ).timestamp())]
    app.game.clock = app.events.clock = app.engagement.clock = lambda: clock[0]
    await feed(msg_update(A, "/start"))
    return app, feed, clock


def publish(app, **values):
    cid = app.events.create_draft(OWNER, {**SEED, "taps": 1, "expeditions": 0, **values})
    app.events.publish(OWNER, cid, 1)
    return cid


async def test_player_can_join_complete_claim_and_find_badge_and_xp_history(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    cid = publish(app)
    await feed(msg_update(A, "🎉 События"))
    assert f"evt:event:{cid}" in callbacks(last_screen(session))
    await feed(cb_update(A, f"evt:event:{cid}"))
    await feed(cb_update(A, f"evt:join:{cid}:2"))
    assert "Клики: 0/1" in last_screen(session).text
    await feed(cb_update(A, "evt:tap"))
    assert "tap" in callbacks(last_screen(session))
    assert app.db.user(A)["balance"] == 0
    await feed(cb_update(A, "tap"))
    await feed(cb_update(A, f"evt:event:{cid}"))
    assert f"evt:claim:{cid}" in callbacks(last_screen(session))
    await feed(cb_update(A, f"evt:claim:{cid}"))
    assert "+40 XP" in session.of(AnswerCallbackQuery)[-1].text
    assert "Награда получена" in last_screen(session).text
    await feed(cb_update(A, f"evt:claim:{cid}"))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert app.db.user(A)["balance"] == 1
    assert app.game.overview(A)["profile"]["xp_total"] == 40
    await feed(cb_update(A, "evt:awards:0"))
    assert SEED["badge"] in last_screen(session).text
    await feed(cb_update(A, f"evt:rank:{cid}"))
    assert "Первые получившие награду" in last_screen(session).text
    await feed(cb_update(A, "adv:collection"))
    assert "Событие: +40 XP" in last_screen(session).text
    counts = {
        r["kind"]: r["n"]
        for r in app.db.all(
            "SELECT kind,COUNT(*) AS n FROM engagement_events WHERE kind IN ('event_join','event_claim','mission') GROUP BY kind"
        )
    }
    assert counts == {"event_join": 1, "event_claim": 1}
    app.db.conn.close()


@pytest.mark.parametrize("kind", ["group", "other_private", "missing", "inaccessible"])
async def test_event_callback_requires_own_accessible_private_chat(tmp_path, bot, session, kind):
    app, feed, clock = await setup(tmp_path, bot)
    cid = publish(app)
    raw = cb_update(A, f"evt:join:{cid}:2").model_dump(exclude_none=True)
    message = raw["callback_query"]
    if kind == "group":
        message["message"]["chat"].update(type="group", id=-123)
    elif kind == "other_private":
        message["message"]["chat"]["id"] = B
    elif kind == "missing":
        message.pop("message")
        message["inline_message_id"] = "synthetic"
    else:
        message["message"]["date"] = 0
        message["message"].pop("text", None)
    before = snapshot(app.db)
    session.clear()
    await feed(Update.model_validate(raw))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert not session.of(EditMessageText)
    assert snapshot(app.db) == before
    app.db.conn.close()


@pytest.mark.parametrize(
    "data",
    [
        "evt:",
        "evt:unknown",
        "evt:home:-1",
        "evt:event:١",
        "evt:claim:" + "9" * 30,
        "evt:join:999:2",
        "evt:manage:1",
        "evt:publish:1:1",
        "evt:claim:1",
        "evt:awards:1000001",
    ],
)
async def test_forged_event_callbacks_cannot_mutate_game(tmp_path, bot, session, data):
    app, feed, clock = await setup(tmp_path, bot)
    before = snapshot(app.db)
    await feed(cb_update(A, data))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    # Gate observations may record interaction; game state remains untouched.
    assert app.db.scalar("SELECT COUNT(*) FROM clicker_profiles") == 0
    assert app.db.scalar("SELECT COUNT(*) FROM clicker_event_players") == 0
    assert app.db.user(A)["balance"] == 0
    assert snapshot(app.db)["clicker_events"] == before["clicker_events"]
    app.db.conn.close()


async def test_opening_events_exits_withdrawal_and_subscription_gate_still_applies(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    app.db.add_balance(A, 20000)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "🎉 События"))
    assert "События" in last_screen(session).text
    await feed(msg_update(A, "10000"))
    assert app.db.scalar("SELECT COUNT(*) FROM withdrawals") == 0
    app.set("required_channel", "@channel")
    session.member_status = "left"
    cid = publish(app)
    await feed(cb_update(A, f"evt:join:{cid}:2"))
    assert app.db.scalar("SELECT COUNT(*) FROM clicker_event_players") == 0
    app.db.conn.close()


async def test_other_templates_have_no_event_schema(tmp_path, bot):
    for template in ("starbot", "refbot"):
        directory = tmp_path / template
        directory.mkdir()
        app, feed = make(directory, template, bot)
        assert app.events is None
        assert not app.db.one("SELECT 1 FROM sqlite_master WHERE name='clicker_events'")
        app.db.conn.close()


async def test_player_screen_handles_maximum_unicode_text_and_callback_limits(tmp_path, bot):
    app, feed, clock = await setup(tmp_path, bot)
    cid = publish(app, title="🌟" * 80, description="🌟" * 900, badge="🌟" * 60)
    text, kb = player_view(app, A, cid)
    visible = html.unescape(re.sub(r"</?(?:b|i|code)>", "", text))
    assert len(visible.encode("utf-16-le")) // 2 <= 4096
    assert all(len(b.callback_data.encode()) <= 64 for row in kb.inline_keyboard for b in row)
    app.db.conn.close()


@pytest.fixture
async def creator_env(tmp_path, bot):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    for uid in (OWNER, B):
        db.upsert_user(uid, f"synthetic{uid}", f"Synthetic {uid}")
    draft = db.create_draft(OWNER, "clicker", "123456:FAKE_CONTENT_TOKEN_NOT_REAL_0123456789", "fixture_bot", "Fixture")
    bid = db.materialize_draft(draft, 1234)
    sup = Supervisor(db, cfg)
    path = sup.write_files(bid)
    app = ChildApp(path)
    app.db.upsert_user(OWNER, None, "Owner", None)
    app.db.add_balance(OWNER, 17.5)
    ctx = Ctx(db, cfg, sup)
    dp = build_dispatcher(ctx)
    w = SimpleNamespace(ctx=ctx, app=app, path=path, id=bid, dp=dp)
    w.open = lambda **kwargs: open_content(ctx, OWNER, bid, "c", **kwargs)
    w.cb = lambda action: callback(ctx.db.bot(bid), action)

    async def feed(update):
        await dp.feed_update(bot, update)

    w.feed = feed
    yield w
    await dp.storage.close()
    app.db.conn.close()
    db.close()


async def press(w, session, prefix):
    data = next(c for c in callbacks(last_screen(session)) if c.startswith(w.cb(prefix)))
    await w.feed(cb_update(OWNER, data))
    return data


async def test_creator_full_editor_preview_publish_pause_copy_and_calendar(creator_env, session):
    w = creator_env
    await w.feed(msg_update(OWNER, "📚 Контент ботов"))
    assert w.cb("home") in callbacks(last_screen(session))
    await w.feed(cb_update(OWNER, w.cb("home")))
    await press(w, session, "new")
    values = {**SEED, "title": "Creator expedition festival", "taps": 10}
    for field in FIELDS:
        await w.feed(msg_update(OWNER, str(values[field])))
    await press(w, session, "field:title:")
    await w.feed(msg_update(OWNER, "Edited festival"))
    save = await press(w, session, "save:")
    cid = w.app.db.scalar("SELECT MAX(id) FROM clicker_events")
    before = snapshot(w.app.db)
    await press(w, session, "preview:")
    assert "Только предпросмотр" in last_screen(session).text
    assert snapshot(w.app.db) == before
    await press(w, session, "manage:")
    await press(w, session, "publish_view:")
    assert w.app.events.owner_event(OWNER, cid)["status"] == "draft"
    await press(w, session, "publish:")
    assert w.app.events.owner_event(OWNER, cid)["title"] == "Edited festival"
    assert w.app.events.owner_event(OWNER, cid)["status"] == "active"
    await w.feed(cb_update(OWNER, save))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    await w.feed(cb_update(OWNER, w.cb(f"manage:{encode(cid)}")))
    await press(w, session, "pause:")
    await press(w, session, "activate:")
    await press(w, session, "copy:")
    copied = w.app.db.scalar("SELECT MAX(id) FROM clicker_events")
    assert w.app.events.owner_event(OWNER, copied)["status"] == "draft"
    await w.feed(cb_update(OWNER, w.cb("calendar:0")))
    assert "Календарь контента" in last_screen(session).text
    assert "Edited festival" in last_screen(session).text
    assert w.app.db.user(OWNER)["balance"] == 17.5
    assert w.app.db.scalar("SELECT COUNT(*) FROM clicker_profiles") == 0


async def test_creator_resume_cancel_and_discard_versions(creator_env, session):
    w = creator_env
    await w.feed(cb_update(OWNER, w.cb("new")))
    await w.feed(msg_update(OWNER, "Saved event title"))
    with w.open() as c:
        old = c.form_get()
    await w.feed(msg_update(OWNER, "/cancel"))
    with w.open() as c:
        assert c.form_get() == old
    await w.feed(cb_update(OWNER, w.cb("resume")))
    await w.feed(msg_update(OWNER, "Continued description"))
    with w.open() as c:
        assert c.form_get()["data"]["title"] == "Saved event title"
        assert c.form_get()["data"]["description"] == "Continued description"
    discard = await press(w, session, "discard:")
    await w.feed(cb_update(OWNER, w.cb("new")))
    await w.feed(cb_update(OWNER, discard))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    with w.open() as c:
        assert c.form_get() is not None


async def test_clicker_content_read_views_are_side_effect_free_and_redact_before_truncation(creator_env):
    w = creator_env
    token = w.ctx.db.bot(w.id)["token"]
    cid = w.app.events.create_draft(OWNER, {**SEED, "title": token})
    before, creator_before = snapshot(w.app.db), snapshot(w.ctx.db)
    with w.open() as c:
        for screen in (views.home(c), views.catalog(c, 0), views.calendar(c, 0), views.owner_card(c, cid), views.preview(c, cid)):
            assert token not in screen[0] and token[:20] not in screen[0]
            assert all(token[:20] not in b.text for row in screen[1].inline_keyboard for b in row)
    assert snapshot(w.app.db) == before
    assert snapshot(w.ctx.db) == creator_before


async def test_clicker_content_prepare_is_explicit_and_does_not_initialize_game(creator_env):
    w = creator_env
    w.app.db.conn.close()
    for suffix in ("", "-wal", "-shm"):
        (w.path / ("bot.db" + suffix)).unlink(missing_ok=True)
    with w.open() as c:
        assert c.engine is None
    assert not (w.path / "bot.db").exists()
    with w.open(write=True, prepare=True) as c:
        assert c.owner_data(1)["status"] == "draft"
        assert not c.db.one("SELECT 1 FROM sqlite_master WHERE name='clicker_profiles'")
        assert not c.db.one("SELECT 1 FROM sqlite_master WHERE name='creator_content_forms'")


async def test_creator_checks_current_owner_on_input_and_new_owner_has_no_previous_form(creator_env, session):
    w = creator_env
    await w.feed(cb_update(OWNER, w.cb("new")))
    with w.open() as c:
        before = c.form_get()
    w.ctx.db.transfer_bot(w.id, B)
    await w.feed(msg_update(OWNER, "must not save"))
    row = w.app.db.one("SELECT * FROM creator_content_forms WHERE owner_id=?", OWNER)
    assert row["version"] == before["version"]
    with pytest.raises(ContentError):
        with open_content(w.ctx, B, w.id, "c"):
            pass
    config = read_config(w.path)
    write_config(w.path, {**config, "owner_id": B})
    with open_content(w.ctx, B, w.id, "c") as c:
        assert c.form_get() is None
        assert c.owner_data(1)
    with pytest.raises(EventError):
        w.app.events.create_draft(OWNER, SEED)


@pytest.mark.parametrize("suffix", ["unknown", "new:extra", "admin:-1", "manage:00", "save:0", "publish:1:0", "field:missing:0"])
async def test_forged_creator_callbacks_do_not_change_clicker_catalog(creator_env, session, suffix):
    w = creator_env
    before = snapshot(w.app.db)
    await w.feed(cb_update(OWNER, w.cb(suffix)))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert snapshot(w.app.db) == before


async def test_creator_event_form_and_catalog_save_are_atomic(creator_env):
    w = creator_env
    with w.open(write=True) as c:
        c.form_begin()
        for field in FIELDS:
            c.form_input(SEED[field], c.form_get()["version"])
        version = c.form_get()["version"]
        c.db.exec("CREATE TRIGGER fault BEFORE DELETE ON creator_content_forms BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        before = snapshot(c.db)
        with pytest.raises(sqlite3.IntegrityError):
            c.form_save(version)
        assert snapshot(c.db) == before


async def test_creator_maximum_text_and_identifiers_fit_telegram(creator_env):
    w = creator_env
    values = {**SEED, "title": "🌟" * 80, "description": "🌟" * 900, "badge": "🌟" * 60}
    cid = w.app.events.create_draft(OWNER, values)
    with w.open(write=True) as c:
        c.form_begin(cid, 1)
        for text, kb in (views.form(c), views.owner_card(c, cid), views.preview(c, cid)):
            visible = html.unescape(re.sub(r"</?(?:b|i|code)>", "", text))
            assert len(visible.encode("utf-16-le")) // 2 <= 4096
            assert all(len(b.callback_data.encode()) <= 64 for row in kb.inline_keyboard for b in row)


def test_largest_event_identifiers_fit_creator_callback_limit():
    maximum = 2**63 - 1
    source = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="publish", callback_data=f"evt:publish_view:{maximum}:{maximum}")],
            [InlineKeyboardButton(text="field", callback_data=f"evt:field:expeditions:{maximum}")],
            [InlineKeyboardButton(text="discard", callback_data="evt:discard")],
        ]
    )
    content = SimpleNamespace(
        code="c", row={"id": maximum, "template": "clicker", "token": ""}, form_get=lambda: {"version": maximum}
    )
    _, keyboard = views.bind(content, "<b>preview</b>", source)
    assert all(len(b.callback_data.encode()) <= 64 for row in keyboard.inline_keyboard for b in row)


async def test_creator_stale_edit_cannot_overwrite_changed_event(creator_env):
    w = creator_env
    with w.open(write=True) as c:
        c.form_begin(1, 1)
    w.app.events.update_draft(OWNER, 1, 1, {**SEED, "title": "Concurrent owner change"})
    with w.open(write=True) as c:
        with pytest.raises(EventError):
            c.form_save(c.form_get()["version"])
        assert c.owner_data(1)["title"] == "Concurrent owner change"


async def test_unreviewed_token_is_rejected_before_clicker_event_publish(creator_env):
    from creator.handlers.content import perform

    w = creator_env
    token = w.ctx.db.bot(w.id)["token"]
    cid = w.app.events.create_draft(OWNER, {**SEED, "description": token})
    with w.open(write=True) as c:
        with pytest.raises(ContentError):
            perform(c, "publish", [encode(cid), encode(1)])
        assert c.owner_data(cid)["status"] == "draft"
