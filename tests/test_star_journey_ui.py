"""The real Star dispatcher with fake Telegram transport, including legacy form escapes."""

import html
import json
import re

import pytest
from aiogram.methods import AnswerCallbackQuery, CopyMessage, EditMessageText, SendMessage
from aiogram.types import Update

from child.files import write_config
from child.star_journey import period
from tests.conftest import cb_update, msg_update
from tests.test_child_bots import OWNER, A, B, make
from tests.test_clicker_adventure_ui import callbacks, last_screen
from tests.test_star_journey import card_data, stamp


async def setup(tmp_path, bot):
    app, feed = make(tmp_path, "starbot", bot)
    clock = [stamp("2026-11-10T09:00:00")]
    app.sky.clock = lambda: clock[0]
    await feed(msg_update(A, "/start"))
    return app, feed, clock


async def click(feed, session, uid, prefix):
    data = next(c for c in callbacks(last_screen(session)) if c.startswith(prefix))
    await feed(cb_update(uid, data))
    return data


async def solve_first_card(app, feed, session, uid=A):
    await feed(cb_update(uid, "sky:deck"))
    await click(feed, session, uid, "sky:card:")
    data = next(c for c in callbacks(last_screen(session)) if c.startswith("sky:answer:"))
    parts = data.split(":")
    parts[3] = str(app.db.scalar("SELECT correct FROM star_cards WHERE id=?", int(parts[2])))
    answer = ":".join(parts)
    await feed(cb_update(uid, answer))
    return answer


async def finish_memory(app, feed, session, uid=A):
    day = period("day", app.sky.now())
    await feed(cb_update(uid, "sky:memory:" + day))
    sequence = json.loads(app.db.scalar("SELECT sequence_json FROM star_memory WHERE user_id=? AND day=?", uid, day))
    await click(feed, session, uid, "sky:ready:")
    assert "Выбери символ №1" in last_screen(session).text
    for symbol in sequence:
        data = next(c for c in callbacks(last_screen(session)) if c.startswith("sky:symbol:") and c.endswith(":" + str(symbol)))
        await feed(cb_update(uid, data))


async def test_complete_first_session_collects_constellation_and_keeps_old_bonus(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    menu = [b.text for row in session.of(SendMessage)[-1].reply_markup.keyboard for b in row]
    assert menu[0] == "🌌 Созвездия" and "🎁 Ежедневный бонус" in menu
    await feed(msg_update(A, "🎁 Ежедневный бонус"))
    assert app.db.user(A)["balance"] == 0.5
    await feed(msg_update(A, "🌌 Созвездия"))
    visit = await click(feed, session, A, "sky:visit:")
    answer = await solve_first_card(app, feed, session)
    await finish_memory(app, feed, session)
    await feed(cb_update(A, "sky:missions:story"))
    await click(feed, session, A, "sky:claim:first_card:")
    await feed(cb_update(A, "sky:map"))
    build = await click(feed, session, A, "sky:build:compass:")
    assert app.sky.overview(A)["built"] == ["compass"]
    assert app.sky.overview(A)["profile"]["sparks"] == 5
    for stale in (visit, answer, build):
        await feed(cb_update(A, stale))
        assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert app.db.user(A)["balance"] == 0.5
    await feed(msg_update(A, "👤 Профиль"))
    assert "Игровые искры не включены в баланс" in last_screen(session).text
    await feed(cb_update(A, "sky:rank:month"))
    assert "55 искр" in last_screen(session).text
    app.db.conn.close()


async def test_full_owner_editor_preview_edit_save_publish_pause_copy(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(OWNER, "/admin"))
    assert "sky:admin:0" in callbacks(last_screen(session))
    await feed(cb_update(OWNER, "sky:new"))
    await click(feed, session, OWNER, "sky:input:category:story:")
    for value in ("Owner story", "The key is blue.", "What color?", "Red\nBlue\nGreen"):
        await feed(msg_update(OWNER, value))
    await click(feed, session, OWNER, "sky:input:correct:1:")
    await feed(msg_update(OWNER, "сегодня"))
    await feed(msg_update(OWNER, "7"))
    assert "Предпросмотр" in last_screen(session).text
    await click(feed, session, OWNER, "sky:field:title:")
    await feed(msg_update(OWNER, "Edited story"))
    await click(feed, session, OWNER, "sky:save:")
    cid = app.db.scalar("SELECT MAX(id) FROM star_cards")
    assert cid == 25 and app.sky.owner_card(OWNER, cid)["status"] == "draft"
    await click(feed, session, OWNER, "sky:edit:")
    await click(feed, session, OWNER, "sky:field:days:")
    await feed(msg_update(OWNER, "2"))
    await click(feed, session, OWNER, "sky:save:")
    await click(feed, session, OWNER, "sky:publish_view:")
    assert app.sky.owner_card(OWNER, cid)["status"] == "draft"
    publish = await click(feed, session, OWNER, "sky:publish:")
    c = app.sky.owner_card(OWNER, cid)
    assert c["title"] == "Edited story" and c["end_day"] == "20261111"
    await feed(cb_update(OWNER, publish))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    await feed(cb_update(OWNER, f"sky:manage:{cid}"))
    await click(feed, session, OWNER, "sky:pause:")
    assert app.sky.owner_card(OWNER, cid)["status"] == "paused"
    await click(feed, session, OWNER, "sky:activate:")
    assert app.sky.owner_card(OWNER, cid)["status"] == "active"
    await click(feed, session, OWNER, "sky:copy:")
    copied = app.db.scalar("SELECT MAX(id) FROM star_cards")
    await feed(cb_update(OWNER, f"sky:copy:{cid}"))
    assert app.db.scalar("SELECT MAX(id) FROM star_cards") == copied
    await feed(cb_update(OWNER, "adm:stats"))
    assert "доступных карточек: 25" in last_screen(session).text
    app.db.conn.close()


@pytest.mark.parametrize(
    "target", ["/start", "🌌 Созвездия", "👤 Профиль", "💸 Вывод", "/admin", "📋 Задания", "🎁 Ежедневный бонус"]
)
async def test_owner_can_leave_resume_and_cancel_content_editor(tmp_path, bot, session, target):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "sky:new"))
    await click(feed, session, OWNER, "sky:input:category:logic:")
    await feed(msg_update(OWNER, "Saved title"))
    await feed(msg_update(OWNER, target))
    assert not app.sky.editor_get(OWNER)["active"]
    await feed(msg_update(OWNER, "ordinary text"))
    assert app.sky.editor_get(OWNER)["step"] == "lesson"
    await feed(cb_update(OWNER, "sky:resume"))
    assert app.sky.editor_get(OWNER)["data"]["title"] == "Saved title"
    assert "шаг 3/8" in last_screen(session).text
    await feed(msg_update(OWNER, "/cancel"))
    assert app.sky.editor_get(OWNER) is None
    app.db.conn.close()


@pytest.mark.parametrize("form", ["withdrawal", "task", "setting", "broadcast"])
async def test_new_menu_exits_legacy_forms_without_saving_or_broadcasting(tmp_path, bot, session, form):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(msg_update(OWNER, "/start"))
    if form == "withdrawal":
        app.db.add_balance(OWNER, 100)
        await feed(msg_update(OWNER, "💸 Вывод"))
    elif form == "task":
        await feed(cb_update(OWNER, "tadd"))
        await feed(msg_update(OWNER, "Synthetic task title"))
    elif form == "setting":
        await feed(cb_update(OWNER, "set:welcome"))
    else:
        await feed(cb_update(OWNER, "adm:bc"))
    original = app.s["welcome"]
    session.clear()
    await feed(msg_update(OWNER, "🌌 Созвездия"))
    assert "Маршрут на" in last_screen(session).text
    await feed(msg_update(OWNER, "ordinary text"))
    assert app.db.scalar("SELECT COUNT(*) FROM tasks") == 0
    assert app.db.scalar("SELECT COUNT(*) FROM withdrawals") == 0
    assert not session.of(CopyMessage)
    assert app.s["welcome"] == original
    app.db.conn.close()


async def test_owner_permission_transfer_is_fresh_for_content_and_text_input(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(msg_update(OWNER, "/start"))
    cid = app.sky.create_draft(OWNER, card_data(app.sky))
    await feed(cb_update(OWNER, "sky:new"))
    write_config(app.dir, {**app.cfg, "owner_id": B})
    await feed(cb_update(OWNER, f"sky:publish:{cid}:1"))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    await feed(msg_update(OWNER, "not accepted by the old editor"))
    assert app.db.scalar("SELECT step FROM star_editor WHERE owner_id=?", OWNER) == "category"
    await feed(cb_update(B, f"sky:publish:{cid}:1"))
    assert app.db.scalar("SELECT status FROM star_cards WHERE id=?", cid) == "active"
    await feed(cb_update(B, "sky:new"))
    await click(feed, session, B, "sky:input:category:story:")
    await feed(msg_update(B, "New owner title"))
    assert app.sky.editor_get(B)["data"]["title"] == "New owner title"
    app.db.conn.close()


@pytest.mark.parametrize(
    "data",
    [
        "sky:",
        "sky:unknown",
        "sky:missions:month",
        "sky:rank:unknown",
        "sky:visit:20200101",
        "sky:card:١:20261110",
        "sky:card:" + "9" * 50 + ":20261110",
        "sky:card:99999:20261110",
        "sky:answer:1:0:1:20261110",
        "sky:memory:old",
        "sky:ready:20261110:0",
        "sky:symbol:20261110:0:100",
        "sky:build:missing:0",
        "sky:claim:missing:once",
        "sky:claim:daily_route:20200101",
        "sky:new",
        "sky:manage:1",
        "sky:publish:1:1",
        "sky:save:0",
        "sky:admin:-1",
    ],
)
async def test_malformed_and_unauthorized_buttons_do_not_award(tmp_path, bot, session, data):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(cb_update(A, data))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert app.sky.overview(A)["profile"]["sparks"] == 0
    assert app.db.user(A)["balance"] == 0
    assert app.db.scalar("SELECT COUNT(*) FROM star_cards") == 24
    app.db.conn.close()


@pytest.mark.parametrize("kind", ["group", "other_private", "missing", "inaccessible"])
async def test_callbacks_require_own_accessible_private_chat(tmp_path, bot, session, kind):
    app, feed, clock = await setup(tmp_path, bot)
    raw = cb_update(A, "sky:home").model_dump(exclude_none=True)
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
    session.clear()
    await feed(Update.model_validate(raw))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert not session.of(EditMessageText)
    assert app.db.scalar("SELECT COUNT(*) FROM star_profiles") == 0
    app.db.conn.close()


@pytest.mark.parametrize(
    "screen",
    [
        "sky:home",
        "sky:deck",
        "sky:memory:20261110",
        "sky:missions:day",
        "sky:missions:week",
        "sky:missions:story",
        "sky:map",
        "sky:rank:month",
        "sky:rank:all",
        "sky:achievements",
        "sky:admin:0",
        "sky:manage:25",
        "sky:edit:25:1",
        "long_card",
    ],
)
async def test_screen_and_callback_size_limits_escape_html(tmp_path, bot, session, screen):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(msg_update(OWNER, "/start"))
    app.set("sky_title", "<script>" + "🌟" * 1000)
    app.set("sky_intro", "<script>" + "🌟" * 3500)
    cid = app.sky.create_draft(
        OWNER,
        card_data(app.sky, title="🌟" * 80, lesson="🌟" * 700, question="🌟" * 180, answers=["🌟" * 80, "🌈" * 80, "🌍" * 80]),
    )
    if screen == "long_card":
        app.sky.publish(OWNER, cid, 1)
        app.db.exec("UPDATE star_cards SET status='paused' WHERE id<>?", cid)
        screen = "sky:card:25:20261110"
    await feed(cb_update(OWNER, screen))
    message = last_screen(session)
    visible = html.unescape(re.sub(r"</?(?:b|i)>", "", message.text))
    assert len(visible.encode("utf-16-le")) // 2 <= 4096
    assert "<script>" not in message.text
    assert all(len(data.encode()) <= 64 for data in callbacks(message))
    assert "sky:home" in callbacks(message) or any(data.startswith("sky:save:") for data in callbacks(message))
    app.db.conn.close()


async def test_required_subscription_still_gates_the_new_game(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    app.set("required_channel", "@channel")
    session.member_status = "left"
    await feed(msg_update(A, "🌌 Созвездия"))
    assert "подпишитесь" in last_screen(session).text
    assert app.db.scalar("SELECT COUNT(*) FROM star_profiles") == 0
    app.db.conn.close()


async def test_other_templates_do_not_initialize_the_star_schema(tmp_path, bot):
    for template in ("clicker", "refbot"):
        path = tmp_path / template
        path.mkdir()
        app, feed = make(path, template, bot)
        await feed(msg_update(A, "/start"))
        assert app.sky is None
        assert not app.db.one("SELECT name FROM sqlite_master WHERE name='star_profiles'")
        app.db.conn.close()
