"""Private child observations and owner reports through real dispatchers with fake Telegram."""

import html
import re
import sqlite3
import time
from types import SimpleNamespace

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from aiogram.types import Update

from child.__main__ import build_dispatcher as child_dispatcher
from child.core import ChildApp
from child.engagement import observer_actor
from child.files import write_config
from creator.app import build_dispatcher
from creator.config import Config
from creator.db import DB
from creator.engagement import read, request
from creator.services import Ctx
from creator.states import CreateBot
from creator.supervisor import Supervisor
from tests.conftest import cb_update, msg_update
from tests.test_clicker_adventure_ui import callbacks, last_screen

OWNER, PLAYER, FRIEND, OTHER = 100, 10, 20, 200


@pytest.fixture(params=["clicker", "refbot", "starbot"])
async def runtime(request, tmp_path, bot, session):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    for uid in (OWNER, OTHER):
        db.upsert_user(uid, "synthetic", "Synthetic owner")
    template = request.param
    draft = db.create_draft(OWNER, template, "123456:FAKE_ANALYTICS_TOKEN_NOT_REAL_123456", "fixture_bot", "Fixture")
    bid = db.materialize_draft(draft, 1234)
    sup = Supervisor(db, cfg)
    path = sup.write_files(bid)
    app = ChildApp(path)
    child_dp = child_dispatcher(app)
    ctx = Ctx(db, cfg, sup)
    creator_dp = build_dispatcher(ctx)

    async def child(update):
        await child_dp.feed_update(bot, update)

    async def creator(update):
        await creator_dp.feed_update(bot, update)

    w = SimpleNamespace(
        ctx=ctx,
        app=app,
        id=bid,
        template=template,
        path=path,
        child=child,
        creator=creator,
        state=creator_dp.fsm.get_context(bot=bot, user_id=OWNER, chat_id=OWNER),
    )
    yield w
    await child_dp.storage.close()
    await creator_dp.storage.close()
    app.db.conn.close()
    db.close()


async def test_private_start_and_template_goal_are_observed_without_owner_activity(runtime, session):
    w = runtime
    await w.child(msg_update(PLAYER, "/start"))
    if w.template == "clicker":
        await w.child(cb_update(PLAYER, "tap"))
        assert w.app.db.user(PLAYER)["balance"] == 1
        goal = "tap"
    elif w.template == "refbot":
        await w.child(cb_update(PLAYER, "club:onboard"))
        await w.child(cb_update(PLAYER, "club:interest:learn:0"))
        await w.child(cb_update(PLAYER, "club:onanswer:1:1"))
        await w.child(cb_update(PLAYER, "club:link:1"))
        goal = "link"
    else:
        await w.child(cb_update(PLAYER, "sky:deck"))
        deck = w.app.sky.deck(PLAYER)
        card = deck["cards"][0]
        await w.child(cb_update(PLAYER, f"sky:card:{card['id']}:{deck['day']}"))
        correct = w.app.db.scalar("SELECT correct FROM star_cards WHERE id=?", card["id"])
        await w.child(cb_update(PLAYER, f"sky:answer:{card['id']}:{correct}:{card['revision']}:{deck['day']}"))
        goal = "card_complete"
    assert w.app.db.scalar("SELECT COUNT(*) FROM engagement_events WHERE user_id=? AND kind=?", PLAYER, goal) == 1
    await w.child(msg_update(OWNER, "/start"))
    await w.child(msg_update(OWNER, "👤 Профиль"))
    assert w.app.db.scalar("SELECT COUNT(*) FROM engagement_people WHERE user_id=?", OWNER) == 0
    report = read(w.path / "bot.db", w.template)
    assert report["active"][1] == report["observed"] == report["registered"] == 1
    assert observer_actor.get() is None
    await w.creator(cb_update(OWNER, request(w.id)))
    assert "Активны сегодня: <b>1</b>" in last_screen(session).text
    assert "Наблюдения с" in last_screen(session).text
    await w.creator(cb_update(OWNER, request(w.id, "steps")))
    assert "Ключевые шаги" in last_screen(session).text


async def test_group_start_is_not_observed(runtime):
    w = runtime
    await w.child(msg_update(PLAYER, "/start", chat={"id": -123, "type": "group"}))
    assert w.app.db.scalar("SELECT COUNT(*) FROM engagement_people") == 0
    assert w.app.db.scalar("SELECT COUNT(*) FROM engagement_activity") == 0
    assert observer_actor.get() is None


async def test_activity_error_does_not_change_existing_reward(runtime, session):
    w = runtime
    await w.child(msg_update(PLAYER, "/start"))
    w.app.db.exec("CREATE TRIGGER fault BEFORE INSERT ON engagement_events BEGIN SELECT RAISE(ABORT,'injected'); END")
    if w.template == "clicker":
        await w.child(cb_update(PLAYER, "tap"))
        assert w.app.db.user(PLAYER)["balance"] == 1
        assert w.app.db.user(PLAYER)["clicks"] == 1
        assert w.app.db.scalar("SELECT COUNT(*) FROM engagement_events WHERE kind='tap'") == 0
    elif w.template == "refbot":
        await w.child(cb_update(PLAYER, "club:interest:learn:0"))
        await w.child(cb_update(PLAYER, "club:onanswer:1:1"))
        assert w.app.club.summary(PLAYER)["member"]["points"] == 20
    else:
        day = w.app.sky.overview(PLAYER)["day"]
        await w.child(cb_update(PLAYER, "sky:visit:" + day))
        assert w.app.sky.overview(PLAYER)["profile"]["sparks"] == 10
    assert observer_actor.get() is None


async def test_report_opening_is_read_only_and_preserves_foreign_wizard(runtime, session):
    w = runtime
    before = w.app.db.conn.total_changes
    await w.creator(cb_update(OWNER, request(w.id)))
    assert w.app.db.conn.total_changes == before
    assert "Впервые наблюдались за период: 0" in last_screen(session).text
    await w.state.set_state(CreateBot.token)
    await w.state.set_data({"template": "starbot"})
    session.clear()
    await w.creator(cb_update(OWNER, request(w.id)))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert not session.of(EditMessageText)
    assert await w.state.get_state() == CreateBot.token.state


async def test_owner_and_matching_current_config_are_required(runtime, session):
    w = runtime
    for uid in (OTHER, PLAYER):
        await w.creator(cb_update(uid, request(w.id)))
        assert session.of(AnswerCallbackQuery)[-1].show_alert
    w.ctx.db.transfer_bot(w.id, OTHER)
    await w.creator(cb_update(OWNER, request(w.id)))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    await w.creator(cb_update(OTHER, request(w.id)))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    write_config(w.path, {**w.app.cfg, "owner_id": OTHER})
    await w.creator(cb_update(OTHER, request(w.id)))
    assert "Активность · бот" in last_screen(session).text


@pytest.mark.parametrize("kind", ["group", "other_private", "missing", "inaccessible"])
async def test_reports_require_own_accessible_private_chat(runtime, session, kind):
    w = runtime
    raw = cb_update(OWNER, request(w.id)).model_dump(exclude_none=True)
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
    await w.creator(Update.model_validate(raw))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert not session.of(EditMessageText)


@pytest.mark.parametrize(
    "raw", ["eng:", "eng:00:overview:30", "eng:1:missing:30", "eng:1:overview:999", "eng:" + "z" * 30 + ":overview:30"]
)
async def test_invalid_callbacks_do_not_mutate_any_observations(runtime, session, raw):
    w = runtime
    before = w.app.db.conn.total_changes
    await w.creator(cb_update(OWNER, raw))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert w.app.db.conn.total_changes == before


async def test_report_text_and_callback_limits_for_all_views(runtime, session):
    w = runtime
    for section in ("overview", "steps", "retention"):
        for days in (7, 30):
            await w.creator(cb_update(OWNER, request(w.id, section, days)))
            screen = last_screen(session)
            visible = html.unescape(re.sub(r"</?(?:b|i)>", "", screen.text))
            assert len(visible.encode("utf-16-le")) // 2 <= 4096
            assert all(len(c.encode()) <= 64 for c in callbacks(screen))


@pytest.mark.parametrize("runtime", ["refbot"], indirect=True)
async def test_qualified_friend_records_progress_without_inviter_visit(runtime):
    w = runtime
    clock = [int(time.time())]
    w.app.engagement.clock = lambda: clock[0]
    await w.child(msg_update(PLAYER, "/start"))
    await w.child(cb_update(PLAYER, "club:interest:learn:0"))
    await w.child(cb_update(PLAYER, "club:onanswer:1:1"))
    await w.child(cb_update(PLAYER, "club:link:1"))
    token = w.app.db.scalar("SELECT token FROM ref_club_links WHERE inviter_id=?", PLAYER)
    clock[0] += 86400
    await w.child(msg_update(FRIEND, "/start c_" + token))
    await w.child(cb_update(FRIEND, "club:interest:learn:0"))
    await w.child(cb_update(FRIEND, "club:onanswer:1:1"))
    await w.child(cb_update(FRIEND, "club:quiz:1"))
    revision = w.app.db.scalar("SELECT revision FROM ref_campaigns WHERE id=1")
    await w.child(cb_update(FRIEND, f"club:answer:1:1:{revision}"))
    assert w.app.db.scalar("SELECT COUNT(*) FROM engagement_activity WHERE user_id=?", PLAYER) == 1
    assert w.app.db.scalar("SELECT COUNT(*) FROM engagement_events WHERE user_id=? AND kind='qualified'", PLAYER) == 1
    assert w.app.db.user(PLAYER)["balance"] == 5
    clock[0] += 86400
    report = read(w.path / "bot.db", "refbot", now=clock[0])
    assert report["retention"][1]["eligible"] == 1 and report["retention"][1]["returned"] == 0


async def test_ownership_change_during_read_blocks_delivery(runtime, session, monkeypatch):
    from creator.handlers import engagement as handler

    w = runtime
    original = handler.read_owned

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        w.ctx.db.transfer_bot(w.id, OTHER)
        return result

    monkeypatch.setattr(handler, "read_owned", changed)
    session.clear()
    await w.creator(cb_update(OWNER, request(w.id)))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert not session.of(EditMessageText)


@pytest.mark.parametrize("runtime", ["clicker"], indirect=True)
async def test_later_business_failure_rolls_back_goal_and_resets_actor_scope(runtime):
    w = runtime
    await w.child(msg_update(PLAYER, "/start"))
    w.app.db.exec("CREATE TRIGGER fault BEFORE UPDATE ON clicker_profiles BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await w.child(cb_update(PLAYER, "tap"))
    assert w.app.db.user(PLAYER)["balance"] == 0
    assert w.app.db.scalar("SELECT COUNT(*) FROM engagement_events WHERE kind='tap'") == 0
    assert observer_actor.get() is None


@pytest.mark.parametrize("template", ["clicker", "refbot", "starbot"])
async def test_domain_calls_outside_private_observation_scope_are_not_visits(tmp_path, bot, template):
    from tests.test_child_bots import make

    app, feed = make(tmp_path, template, bot)
    app.db.upsert_user(PLAYER, None, "Synthetic player", None)
    if template == "clicker":
        app.game.tap(PLAYER, base_reward=1)
    elif template == "refbot":
        app.club.choose_interest(PLAYER, "learn", 0)
        app.club.finish_onboarding(PLAYER, 1, 1)
    else:
        app.sky.checkin(PLAYER, app.sky.overview(PLAYER)["day"])
    assert app.db.scalar("SELECT COUNT(*) FROM engagement_activity") == 0
    app.db.conn.close()
