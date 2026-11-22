"""Player teams and owner reports through real dispatchers with fake Telegram sessions."""

import html
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from aiogram.types import Update

from child.__main__ import build_dispatcher as child_dispatcher
from child.core import ChildApp
from child.files import read_config, write_config
from child.referral_teams import SCHEMA, TZ, TeamError
from child.referral_teams_views import form_view, team_view
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
from tests.test_referral_club_ui import click, onboard
from tests.test_referral_teams import create
from tests.test_referral_teams import onboard as onboard_engine


async def setup(tmp_path, bot, session):
    app, feed = make(tmp_path, "refbot", bot)
    clock = [int(datetime(2026, 11, 9, 12, tzinfo=TZ).timestamp())]
    app.club.clock = app.teams.clock = app.engagement.clock = lambda: clock[0]
    await feed(msg_update(A, "/start"))
    await onboard(feed, session, A)
    return app, feed, clock


async def test_complete_cooperative_week_and_collection_in_telegram(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot, session)
    await feed(msg_update(A, "🤝 Команды"))
    await feed(cb_update(A, "rt:new"))
    await feed(msg_update(A, "Неоновые исследователи"))
    assert "Создать «Неоновые исследователи»" in last_screen(session).text
    await click(feed, session, A, "rt:create:")
    tid = app.teams.membership(A)["team_id"]
    await feed(msg_update(B, "/start"))
    await onboard(feed, session, B)
    await feed(cb_update(B, f"rt:join_view:{tid}"))
    assert "до конца этой недели" in last_screen(session).text
    await click(feed, session, B, "rt:join:")
    for uid in (30, 40, 50):
        onboard_engine(app.club, uid)
        app.teams.join(uid, tid, 1)
    for _ in range(3):
        for uid in (A, B):
            await feed(cb_update(uid, f"rt:checkin:{tid}"))
        for uid in (30, 40, 50):
            app.teams.checkin(uid, tid)
        clock[0] += 86400
    await feed(cb_update(A, f"rt:view:{tid}"))
    assert f"rt:claim:{tid}" in callbacks(last_screen(session))
    await feed(cb_update(A, f"rt:claim:{tid}"))
    assert "значок получен" in session.of(AnswerCallbackQuery)[-1].text
    await feed(cb_update(A, f"rt:claim:{tid}"))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    await feed(cb_update(A, "rt:awards:0"))
    assert "Неоновые исследователи" in last_screen(session).text
    await feed(cb_update(A, "rt:rank:0"))
    assert "15 очков" in last_screen(session).text
    assert app.db.user(A)["balance"] == 0
    assert app.club.summary(A)["member"]["points"] == 20
    counts = {
        r["kind"]: r["n"]
        for r in app.db.all("SELECT kind,COUNT(*) AS n FROM engagement_events WHERE kind LIKE 'team_%' GROUP BY kind")
    }
    assert counts == {"team_join": 2, "team_checkin": 6, "team_claim": 1}
    app.db.conn.close()


async def test_unclaimed_old_team_is_accessible_through_my_weeks(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot, session)
    w = SimpleNamespace(teams=app.teams, db=app.db)
    tid = create(w)
    for uid in (B, 30, 40, 50):
        onboard_engine(app.club, uid)
        app.teams.join(uid, tid, 1)
    for _ in range(3):
        for uid in (A, B, 30, 40, 50):
            app.teams.checkin(uid, tid)
        clock[0] += 86400
    clock[0] = app.teams.team(A, tid)["end_at"]
    await feed(msg_update(A, "🤝 Команды"))
    assert app.teams.membership(A) is None
    await feed(cb_update(A, "rt:history:0"))
    assert f"rt:view:{tid}" in callbacks(last_screen(session))
    await feed(cb_update(A, f"rt:view:{tid}"))
    assert f"rt:claim:{tid}" in callbacks(last_screen(session))
    await feed(cb_update(A, f"rt:claim:{tid}"))
    assert app.teams.awards(A)["total"] == 1
    app.db.conn.close()


async def test_form_resumes_after_dispatcher_restart_and_main_navigation_pauses_input(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot, session)
    await feed(cb_update(A, "rt:new"))
    other = ChildApp(tmp_path)
    other.club.clock = other.teams.clock = lambda: clock[0]
    dp = child_dispatcher(other)
    await dp.feed_update(bot, msg_update(A, "Сохранённое название"))
    assert other.teams.form_get(A)["name"] == "Сохранённое название"
    before = other.teams.form_get(A)
    await dp.feed_update(bot, msg_update(A, "🚀 Клуб"))
    await dp.feed_update(bot, msg_update(A, "не сохранять"))
    assert other.teams.form_get(A)["name"] == before["name"]
    assert not other.teams.form_get(A)["active"]
    await dp.feed_update(bot, cb_update(A, "rt:new"))
    await click(lambda update: dp.feed_update(bot, update), session, A, "rt:edit:")
    await dp.feed_update(bot, msg_update(A, "Исправленное название"))
    assert other.teams.form_get(A)["name"] == "Исправленное название"
    await dp.feed_update(bot, msg_update(A, "/cancel"))
    assert other.teams.form_get(A) is None
    await dp.storage.close()
    other.db.conn.close()
    app.db.conn.close()


async def test_owner_team_form_does_not_overwrite_campaign_wizard(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot, session)
    await feed(msg_update(OWNER, "/start"))
    await onboard(feed, session, OWNER)
    await feed(cb_update(OWNER, "club:new"))
    await feed(msg_update(OWNER, "Campaign draft"))
    original = app.club.wizard_get(OWNER)["data"]
    await feed(msg_update(OWNER, "🤝 Команды"))
    await feed(cb_update(OWNER, "rt:new"))
    await feed(msg_update(OWNER, "Owner team"))
    assert app.teams.form_get(OWNER)["name"] == "Owner team"
    assert app.club.wizard_get(OWNER)["data"] == original
    await feed(msg_update(OWNER, "/cancel"))
    assert app.teams.form_get(OWNER) is None
    assert app.club.wizard_get(OWNER)["data"] == original
    app.db.conn.close()


@pytest.mark.parametrize("kind", ["group", "other_private", "missing", "inaccessible"])
async def test_team_callbacks_require_own_accessible_private_chat(tmp_path, bot, session, kind):
    app, feed, clock = await setup(tmp_path, bot, session)
    raw = cb_update(A, "rt:new").model_dump(exclude_none=True)
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
        "rt:",
        "rt:unknown",
        "rt:list:-1",
        "rt:rank:1000001",
        "rt:view:١",
        "rt:view:" + "9" * 30,
        "rt:join:999:1",
        "rt:claim:999",
        "rt:checkin:999",
        "rt:create:0",
        "rt:admin:0:0",
    ],
)
async def test_forged_team_callbacks_have_safe_errors_and_no_game_writes(tmp_path, bot, session, data):
    app, feed, clock = await setup(tmp_path, bot, session)
    await feed(cb_update(A, data))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert app.db.scalar("SELECT COUNT(*) FROM ref_teams") == 0
    assert app.db.scalar("SELECT COUNT(*) FROM ref_team_members") == 0
    assert app.db.user(A)["balance"] == 0
    app.db.conn.close()


async def test_teams_escape_withdrawal_and_preserve_required_channel_gate(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot, session)
    app.db.add_balance(A, 20000)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "🤝 Команды"))
    assert "Команды недели" in last_screen(session).text
    await feed(msg_update(A, "10000"))
    assert app.db.scalar("SELECT COUNT(*) FROM withdrawals") == 0
    app.set("required_channel", "@channel")
    session.member_status = "left"
    await feed(cb_update(A, "rt:new"))
    assert app.teams.form_get(A) is None
    app.db.conn.close()


async def test_other_templates_do_not_create_team_tables(tmp_path, bot):
    for template in ("clicker", "starbot"):
        directory = tmp_path / template
        directory.mkdir()
        app, feed = make(directory, template, bot)
        assert app.teams is None
        assert not app.db.one("SELECT 1 FROM sqlite_master WHERE name='ref_teams'")
        app.db.conn.close()


async def test_team_text_is_escaped_and_telegram_limits_hold(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot, session)
    w = SimpleNamespace(teams=app.teams)
    f = app.teams.form_begin(A)
    app.teams.form_name(A, "<b>&" + "🌟" * 44, f["version"])
    form_text, form_kb = form_view(app, A)
    tid = app.teams.create(A, app.teams.form_get(A)["version"])
    for text, keyboard in ((form_text, form_kb), team_view(app, A, tid)):
        assert "&lt;b&gt;&amp;" in text
        visible = html.unescape(re.sub(r"</?(?:b|i|code)>", "", text))
        assert len(visible.encode("utf-16-le")) // 2 <= 4096
        assert all(len(b.callback_data.encode()) <= 64 for row in keyboard.inline_keyboard for b in row)
    assert w.teams.team(A, tid)["name"].startswith("<b>&")
    app.db.conn.close()


@pytest.fixture
async def owner_env(tmp_path, bot):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    for uid in (OWNER, B):
        db.upsert_user(uid, f"synthetic{uid}", f"Synthetic {uid}")
    draft = db.create_draft(OWNER, "refbot", "123456:FAKE_CONTENT_TOKEN_NOT_REAL_0123456789", "fixture_bot", "Fixture")
    bid = db.materialize_draft(draft, 1234)
    sup = Supervisor(db, cfg)
    path = sup.write_files(bid)
    app = ChildApp(path)
    onboard_engine(app.club, A)
    tid = create(SimpleNamespace(teams=app.teams))
    ctx = Ctx(db, cfg, sup)
    dp = build_dispatcher(ctx)
    w = SimpleNamespace(ctx=ctx, app=app, id=bid, tid=tid, path=path)
    w.open = lambda **kwargs: open_content(ctx, OWNER, bid, "r", **kwargs)
    w.cb = lambda action: callback(ctx.db.bot(bid), action)

    async def feed(update):
        await dp.feed_update(bot, update)

    w.feed = feed
    yield w
    await dp.storage.close()
    app.db.conn.close()
    db.close()


async def test_creator_team_overview_pause_resume_and_history_preserve_other_data(owner_env, session):
    w = owner_env
    with w.open(write=True) as c:
        c.form_begin()
        form = c.form_get()
    await w.feed(cb_update(OWNER, w.cb("home")))
    assert w.cb("teams:0:0") in callbacks(last_screen(session))
    before = snapshot(w.app.db)
    await w.feed(cb_update(OWNER, w.cb("teams:0:0")))
    assert "Команды клуба" in last_screen(session).text
    assert snapshot(w.app.db) == before
    await w.feed(cb_update(OWNER, w.cb("team:" + encode(w.tid))))
    pause = next(c for c in callbacks(last_screen(session)) if c.startswith(w.cb("team_pause:")))
    await w.feed(cb_update(OWNER, pause))
    assert w.app.teams.owner_team(OWNER, w.tid)["status"] == "paused"
    await w.feed(cb_update(OWNER, pause))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    resume = next(c for c in callbacks(last_screen(session)) if c.startswith(w.cb("team_activate:")))
    await w.feed(cb_update(OWNER, resume))
    assert w.app.teams.owner_team(OWNER, w.tid)["status"] == "open"
    await w.feed(cb_update(OWNER, w.cb("teams:1:0")))
    assert "ещё не создавались" in last_screen(session).text
    with w.open() as c:
        assert c.form_get() == form


async def test_creator_requires_explicit_team_prepare_on_older_child_catalog(owner_env, session):
    w = owner_env
    names = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA)
    for name in names:
        w.app.db.exec(f'DROP TABLE "{name}"')
    before = snapshot(w.app.db)
    await w.feed(cb_update(OWNER, w.cb("teams:0:0")))
    assert "ещё не подготовлен" in last_screen(session).text
    assert snapshot(w.app.db) == before
    await w.feed(cb_update(OWNER, w.cb("team_prepare")))
    assert "Команды клуба" in last_screen(session).text
    assert w.app.db.scalar("SELECT COUNT(*) FROM ref_teams") == 0
    after = snapshot(w.app.db)
    assert all(after[name] == rows for name, rows in before.items())


@pytest.mark.parametrize("problem", ["transfer", "config", "template", "symlink"])
async def test_creator_team_mutations_recheck_current_owner_identity_and_paths(owner_env, session, tmp_path, problem):
    w = owner_env
    if problem == "transfer":
        w.ctx.db.transfer_bot(w.id, B)
    elif problem == "config":
        config = read_config(w.path)
        write_config(w.path, {**config, "username": "different_bot"})
    elif problem == "template":
        w.ctx.db.update_bot(w.id, template="starbot")
    else:
        w.app.db.conn.close()
        other = tmp_path / "unrelated.db"
        other.write_bytes((w.path / "bot.db").read_bytes())
        (w.path / "bot.db").unlink()
        (w.path / "bot.db").symlink_to(other)
    await w.feed(cb_update(OWNER, w.cb(f"team_pause:{encode(w.tid)}:1")))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    with pytest.raises(ContentError):
        with w.open():
            pass


async def test_owner_views_redact_known_secrets_before_names_are_truncated(owner_env):
    w = owner_env
    token = w.ctx.db.bot(w.id)["token"]
    w.app.db.exec("UPDATE ref_teams SET name=? WHERE id=?", token, w.tid)
    w.app.db.exec("UPDATE users SET full_name=? WHERE id=?", token, A)
    with w.open() as c:
        engine = c.team_catalog()
        before = snapshot(c.db)
        for text, keyboard in (views.team_screen(c, engine), views.team_screen(c, engine, tid=w.tid)):
            assert token not in text and token[:20] not in text
            assert all(token[:20] not in b.text for row in keyboard.inline_keyboard for b in row)
        assert snapshot(c.db) == before


@pytest.mark.parametrize("suffix", ["teams:0", "teams:-1:0", "teams:1h:0", "team:00", "team_pause:1:0", "team_prepare:extra"])
async def test_forged_creator_team_buttons_do_not_change_team_catalog(owner_env, session, suffix):
    w = owner_env
    before = snapshot(w.app.db)
    await w.feed(cb_update(OWNER, w.cb(suffix)))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert snapshot(w.app.db) == before


async def test_native_previous_owner_cannot_pause_teams_after_transfer(owner_env):
    w = owner_env
    config = read_config(w.path)
    write_config(w.path, {**config, "owner_id": B})
    with pytest.raises(TeamError):
        w.app.teams.set_status(OWNER, w.tid, "paused", 1)
