"""Native track sessions and Creator source picking with fake Telegram and real child files."""

import html
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from aiogram.types import Update

from child.core import ChildApp
from child.files import read_config, write_config
from child.star_journey import TZ
from child.star_tracks import SCHEMA
from child.star_tracks_views import stage_view
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
from tests.test_star_tracks import VALUES, finish, publish


async def setup(tmp_path, bot):
    app, feed = make(tmp_path, "starbot", bot)
    clock = [int(datetime(2026, 11, 11, 12, tzinfo=TZ).timestamp())]
    app.sky.clock = app.tracks.clock = app.engagement.clock = lambda: clock[0]
    await feed(msg_update(A, "/start"))
    return app, feed, clock


async def test_native_sequential_track_claim_badge_and_repeat_material(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    tid = publish(SimpleNamespace(tracks=app.tracks))
    await feed(msg_update(A, "🧭 Маршруты"))
    await feed(cb_update(A, f"trk:view:{tid}"))
    await feed(cb_update(A, f"trk:join:{tid}:2"))
    for pos in range(3):
        await feed(cb_update(A, f"trk:stage:{tid}:{pos}"))
        choice = app.db.scalar("SELECT correct FROM star_track_stages WHERE track_id=? AND position=?", tid, pos)
        data = next(c for c in callbacks(last_screen(session)) if c.startswith(f"trk:answer:{tid}:{pos}:{choice}:"))
        await feed(cb_update(A, data))
        assert "Этап пройден" in session.of(AnswerCallbackQuery)[-1].text
    await feed(cb_update(A, f"trk:claim:{tid}"))
    assert "Значок получен" in session.of(AnswerCallbackQuery)[-1].text
    await feed(cb_update(A, f"trk:claim:{tid}"))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    await feed(cb_update(A, "trk:awards:0"))
    assert VALUES["badge"] in last_screen(session).text
    await feed(cb_update(A, f"trk:stage:{tid}:0"))
    assert "Повторение не меняет результат" in last_screen(session).text
    assert not any(c.startswith("trk:answer:") for c in callbacks(last_screen(session)))
    assert app.db.user(A)["balance"] == 0
    assert app.db.scalar("SELECT COUNT(*) FROM star_profiles") == 0
    assert app.db.scalar("SELECT COUNT(*) FROM star_sparks") == 0
    events = {
        r["kind"]: r["n"]
        for r in app.db.all("SELECT kind,COUNT(*) AS n FROM engagement_events WHERE kind LIKE 'track_%' GROUP BY kind")
    }
    assert events == {"track_join": 1, "track_step": 1, "track_complete": 1, "track_claim": 1}
    app.db.conn.close()


async def test_wrong_answer_stale_callback_and_next_stage_are_guarded(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    tid = publish(SimpleNamespace(tracks=app.tracks))
    app.tracks.join(A, tid, 2)
    await feed(cb_update(A, f"trk:stage:{tid}:1"))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    await feed(cb_update(A, f"trk:stage:{tid}:0"))
    correct = app.db.scalar("SELECT correct FROM star_track_stages WHERE track_id=? AND position=0", tid)
    wrong = next(c for c in callbacks(last_screen(session)) if c.startswith(f"trk:answer:{tid}:0:{(correct + 1) % 3}:"))
    await feed(cb_update(A, wrong))
    assert "Повтор через 30" in last_screen(session).text
    await feed(cb_update(A, wrong))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert app.tracks.player_track(A, tid)["player"]["current_step"] == 0
    app.db.conn.close()


async def test_my_tracks_keeps_unclaimed_route_after_end_and_review_after_grace(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    w = SimpleNamespace(tracks=app.tracks, db=app.db)
    tid = publish(w, days=1)
    app.tracks.join(A, tid, 2)
    finish(w, tid)
    clock[0] = app.tracks.player_track(A, tid)["ends_at"]
    await feed(cb_update(A, "trk:mine:0"))
    assert f"trk:view:{tid}" in callbacks(last_screen(session))
    await feed(cb_update(A, f"trk:view:{tid}"))
    assert f"trk:claim:{tid}" in callbacks(last_screen(session))
    await feed(cb_update(A, f"trk:claim:{tid}"))
    clock[0] += 8 * 86400
    await feed(cb_update(A, f"trk:stage:{tid}:2"))
    assert "Этот этап пройден" in last_screen(session).text
    app.db.conn.close()


@pytest.mark.parametrize("kind", ["group", "other_private", "missing", "inaccessible"])
async def test_track_callbacks_require_own_accessible_private_chat(tmp_path, bot, session, kind):
    app, feed, clock = await setup(tmp_path, bot)
    tid = publish(SimpleNamespace(tracks=app.tracks))
    raw = cb_update(A, f"trk:join:{tid}:2").model_dump(exclude_none=True)
    q = raw["callback_query"]
    if kind == "group":
        q["message"]["chat"].update(type="group", id=-123)
    elif kind == "other_private":
        q["message"]["chat"]["id"] = B
    elif kind == "missing":
        q.pop("message")
        q["inline_message_id"] = "synthetic"
    else:
        q["message"]["date"] = 0
        q["message"].pop("text", None)
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
        "trk:",
        "trk:unknown",
        "trk:list:-1",
        "trk:mine:1000001",
        "trk:view:١",
        "trk:view:" + "9" * 30,
        "trk:join:999:2",
        "trk:stage:999:0",
        "trk:answer:1:0:3:1",
        "trk:claim:999",
        "trk:publish:1:1",
        "trk:awards:1000001",
    ],
)
async def test_forged_native_buttons_do_not_create_progress_or_rewards(tmp_path, bot, session, data):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(cb_update(A, data))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert not app.db.scalar("SELECT COUNT(*) FROM star_track_players")
    assert not app.db.scalar("SELECT COUNT(*) FROM star_track_answers")
    assert app.db.user(A)["balance"] == 0
    app.db.conn.close()


async def test_routes_exit_withdrawal_task_wizard_and_preserve_subscription_gate(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    app.db.add_balance(A, 100)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "🧭 Маршруты"))
    await feed(msg_update(A, "20"))
    assert app.db.scalar("SELECT COUNT(*) FROM withdrawals") == 0
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "tadd"))
    await feed(msg_update(OWNER, "🧭 Маршруты"))
    assert "Тематические маршруты" in last_screen(session).text
    assert app.db.scalar("SELECT COUNT(*) FROM tasks") == 0
    tid = publish(SimpleNamespace(tracks=app.tracks))
    app.set("required_channel", "@channel")
    session.member_status = "left"
    await feed(cb_update(A, f"trk:join:{tid}:2"))
    assert not app.db.scalar("SELECT COUNT(*) FROM star_track_players")
    app.db.conn.close()


async def test_other_templates_do_not_initialize_track_tables(tmp_path, bot):
    for template in ("clicker", "refbot"):
        directory = tmp_path / template
        directory.mkdir()
        app, feed = make(directory, template, bot)
        assert app.tracks is None
        assert not app.db.one("SELECT 1 FROM sqlite_master WHERE name='star_tracks'")
        app.db.conn.close()


@pytest.fixture
async def owner_env(tmp_path, bot):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    for uid in (OWNER, B):
        db.upsert_user(uid, f"synthetic{uid}", f"Synthetic {uid}")
    draft = db.create_draft(OWNER, "starbot", "123456:FAKE_CONTENT_TOKEN_NOT_REAL_0123456789", "fixture_bot", "Fixture")
    bid = db.materialize_draft(draft, 1234)
    sup = Supervisor(db, cfg)
    path = sup.write_files(bid)
    app = ChildApp(path)
    ctx = Ctx(db, cfg, sup)
    dp = build_dispatcher(ctx)
    w = SimpleNamespace(ctx=ctx, app=app, id=bid, path=path, bot=bot, dp=dp)
    w.open = lambda code="t", **kwargs: open_content(ctx, OWNER, bid, code, **kwargs)
    w.cb = lambda action, code="t": callback(ctx.db.bot(bid), action, code=code)
    w.state = dp.fsm.get_context(bot=bot, user_id=OWNER, chat_id=OWNER)

    async def feed(update):
        await w.dp.feed_update(bot, update)

    w.feed = feed
    yield w
    await w.dp.storage.close()
    app.db.conn.close()
    db.close()


async def press(w, session, prefix):
    value = next(c for c in callbacks(last_screen(session)) if c.startswith(w.cb(prefix)))
    await w.feed(cb_update(OWNER, value))
    return value


async def fill(w, session, *, buttons=True):
    await w.feed(cb_update(OWNER, w.cb("new")))
    await w.feed(msg_update(OWNER, "Creator learning route"))
    await w.feed(msg_update(OWNER, "Read, think and collect a badge."))
    if buttons:
        for cid in (3, 1, 2):
            await press(w, session, f"pick:{encode(cid)}:")
        await press(w, session, "picked:")
    else:
        await w.feed(msg_update(OWNER, "3, 1, 2"))
    for value in ("сегодня", "7", "🎓 Пройденный маршрут"):
        await w.feed(msg_update(OWNER, value))


async def test_creator_full_route_editor_picker_preview_publish_and_independent_forms(owner_env, session):
    w = owner_env
    with w.open("s", write=True) as c:
        c.form_begin()
        c.form_input("logic", c.form_get()["version"])
        old_form = c.form_get()
    w.app.sky.editor_begin(OWNER)
    old_native = w.app.sky.editor_get(OWNER)
    await w.feed(cb_update(OWNER, w.cb("home", code="s")))
    assert w.cb("home") in callbacks(last_screen(session))
    await fill(w, session)
    assert (await w.state.get_data())["content_kind"] == "t"
    await press(w, session, "save:")
    tid = w.app.db.scalar("SELECT MAX(id) FROM star_tracks")
    assert w.app.tracks.owner_track(OWNER, tid)["cards"] == [3, 1, 2]
    before = snapshot(w.app.db)
    await press(w, session, "preview:")
    await press(w, session, "preview_step:")
    assert "Только предпросмотр" in last_screen(session).text
    assert "✅" not in last_screen(session).text
    assert snapshot(w.app.db) == before
    await press(w, session, "manage:")
    await press(w, session, "publish_view:")
    await press(w, session, "publish:")
    assert w.app.tracks.owner_track(OWNER, tid)["status"] == "active"
    await press(w, session, "pause:")
    await press(w, session, "activate:")
    await press(w, session, "copy:")
    assert w.app.db.scalar("SELECT MAX(id) FROM star_tracks") != tid
    await w.feed(cb_update(OWNER, w.cb("calendar:0")))
    assert "Creator learning route" in last_screen(session).text
    with w.open("s") as c:
        assert c.form_get() == old_form
    assert w.app.sky.editor_get(OWNER) == old_native
    assert w.app.db.scalar("SELECT COUNT(*) FROM star_profiles") == 0


async def test_picker_stale_buttons_order_removal_and_page_are_persistent(owner_env, session):
    w = owner_env
    await w.feed(cb_update(OWNER, w.cb("new")))
    for value in ("Route", "Description"):
        await w.feed(msg_update(OWNER, value))
    stale = await press(w, session, "pick:1:")
    await w.feed(cb_update(OWNER, stale))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    await press(w, session, "pick:2:")
    await press(w, session, "pick:1:")
    with w.open() as c:
        assert c.form_get()["data"]["cards"] == [2]
    await press(w, session, "sources:1:")
    await press(w, session, "pick:7:")
    assert "страница 2" in last_screen(session).text
    with w.open() as c:
        assert c.form_get()["data"]["cards"] == [2, 7]


async def test_route_form_survives_cancel_and_new_dispatcher_without_capturing_card_text(owner_env, session):
    w = owner_env
    await w.feed(cb_update(OWNER, w.cb("new")))
    await w.feed(msg_update(OWNER, "Persistent route"))
    await w.feed(msg_update(OWNER, "/cancel"))
    await w.dp.storage.close()
    w.dp = build_dispatcher(w.ctx)
    w.state = w.dp.fsm.get_context(bot=w.bot, user_id=OWNER, chat_id=OWNER)
    await w.feed(cb_update(OWNER, w.cb("resume")))
    await w.feed(msg_update(OWNER, "Resumed route description"))
    with w.open() as c:
        assert c.form_get()["data"]["description"] == "Resumed route description"
    with w.open("s") as c:
        assert c.form_get() is None


async def test_routes_prepare_is_explicit_and_requires_card_catalog_first(owner_env, session):
    w = owner_env
    for table in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA):
        w.app.db.exec(f'DROP TABLE "{table}"')
    before = snapshot(w.app.db)
    await w.feed(cb_update(OWNER, w.cb("home")))
    assert "не подготовлен" in last_screen(session).text
    assert snapshot(w.app.db) == before
    await w.feed(cb_update(OWNER, w.cb("prepare")))
    assert w.app.db.scalar("SELECT COUNT(*) FROM star_tracks WHERE status='draft'") == 1
    assert w.app.db.scalar("SELECT COUNT(*) FROM star_track_players") == 0


async def test_missing_card_database_cannot_be_created_by_track_preview_or_prepare(owner_env):
    w = owner_env
    w.app.db.conn.close()
    for suffix in ("", "-wal", "-shm"):
        (w.path / ("bot.db" + suffix)).unlink(missing_ok=True)
    with w.open() as c:
        assert c.engine is None
        views.home(c)
    assert not (w.path / "bot.db").exists()
    with pytest.raises(ContentError):
        with w.open(write=True, prepare=True):
            pass
    assert not (w.path / "bot.db").exists()


async def test_owner_preview_and_picker_redact_sources_and_publish_rejects_unreviewed_secret(owner_env):
    from creator.handlers.content import perform

    w = owner_env
    token = w.ctx.db.bot(w.id)["token"]
    w.app.db.exec("UPDATE star_cards SET title=?,lesson=? WHERE id=1", token, token)
    tid = w.app.tracks.create_draft(OWNER, VALUES)
    with w.open(write=True) as c:
        for text, keyboard in (views.owner_card(c, tid), views.preview(c, tid), views.track_preview(c, tid, 0)):
            assert token not in text and token[:20] not in text
            assert all(token[:20] not in b.text for row in keyboard.inline_keyboard for b in row)
        before = snapshot(c.db)
        with pytest.raises(ContentError):
            perform(c, "publish", [encode(tid), "1"])
        assert snapshot(c.db) == before


async def test_route_form_save_and_delete_are_one_transaction(owner_env):
    import sqlite3

    w = owner_env
    with w.open(write=True) as c:
        c.form_begin()
        for field in c.fields:
            c.form_input(VALUES[field], c.form_get()["version"])
        version = c.form_get()["version"]
        c.db.exec("CREATE TRIGGER fail BEFORE DELETE ON creator_track_forms BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        before = snapshot(c.db)
        with pytest.raises(sqlite3.IntegrityError):
            c.form_save(version)
        assert snapshot(c.db) == before


@pytest.mark.parametrize("problem", ["transfer", "config", "template", "symlink"])
async def test_creator_track_actions_recheck_ownership_and_path(owner_env, session, tmp_path, problem):
    w = owner_env
    if problem == "transfer":
        w.ctx.db.transfer_bot(w.id, B)
    elif problem == "config":
        cfg = read_config(w.path)
        write_config(w.path, {**cfg, "username": "other_bot"})
    elif problem == "template":
        w.ctx.db.update_bot(w.id, template="refbot")
    else:
        w.app.db.conn.close()
        other = tmp_path / "unrelated.db"
        other.write_bytes((w.path / "bot.db").read_bytes())
        (w.path / "bot.db").unlink()
        (w.path / "bot.db").symlink_to(other)
    data = f"cnt:{encode(w.id)}:t:new"
    await w.feed(cb_update(OWNER, data))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    with pytest.raises(ContentError):
        with w.open():
            pass


async def test_maximum_unicode_text_and_identifiers_fit_telegram(owner_env):
    w = owner_env
    values = {**VALUES, "title": "🌟" * 80, "description": "🌟" * 900, "badge": "🌟" * 60, "cards": list(range(1, 13))}
    tid = w.app.tracks.create_draft(OWNER, values)
    with w.open(write=True) as c:
        c.form_begin(tid, 1)
        for text, keyboard in (
            views.form(c),
            views.owner_card(c, tid, confirm=True),
            views.preview(c, tid),
            views.track_preview(c, tid, 0),
        ):
            visible = html.unescape(re.sub(r"</?(?:b|i|code)>", "", text))
            assert len(visible.encode("utf-16-le")) // 2 <= 4096
            assert all(len(b.callback_data.encode()) <= 64 for row in keyboard.inline_keyboard for b in row)
    w.app.tracks.publish(OWNER, tid, 1)
    w.app.db.upsert_user(A, None, "Player", None)
    w.app.tracks.join(A, tid, 2)
    w.app.db.exec("UPDATE star_track_players SET version=? WHERE user_id=?", 2**63 - 1, A)
    _, keyboard = stage_view(w.app, A, tid, 0)
    assert all(len(b.callback_data.encode()) <= 64 for row in keyboard.inline_keyboard for b in row)


async def test_send_failure_preserves_route_form_for_resume(owner_env, session, monkeypatch):
    w = owner_env
    original = session.make_request

    async def fail(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: cannot edit")
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", fail)
    await w.feed(cb_update(OWNER, w.cb("new")))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    with w.open() as c:
        assert c.form_get() is not None
    monkeypatch.setattr(session, "make_request", original)
    await w.feed(cb_update(OWNER, w.cb("resume")))
    await w.feed(msg_update(OWNER, "Recovered route title"))
    with w.open() as c:
        assert c.form_get()["data"]["title"] == "Recovered route title"
