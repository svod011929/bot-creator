"""Real dispatcher paths with a fake Telegram session, including legacy balance compatibility."""
import html
import re

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Update

from tests.conftest import cb_update, msg_update
from tests.test_child_bots import OWNER, A, B, make


def callbacks(screen):
    return [b.callback_data for row in screen.reply_markup.inline_keyboard for b in row if b.callback_data]


def last_screen(session):
    return next(m for m in reversed(session.requests) if isinstance(m, (EditMessageText, SendMessage)))


async def setup(tmp_path, bot):
    app, feed = make(tmp_path, "clicker", bot)
    clock = [1794301200]
    app.game.clock = lambda: clock[0]
    await feed(msg_update(A, "/start"))
    return app, feed, clock


async def test_complete_first_session_in_telegram(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    assert "🧭 Приключение" in [b.text for row in session.of(SendMessage)[-1].reply_markup.keyboard for b in row]
    await feed(msg_update(A, "🧭 Приключение"))
    assert "Экспедиция" in last_screen(session).text
    assert "adv:missions:story" in callbacks(last_screen(session))
    # Actual tap accounting is shared with the router; no waiting or real Telegram calls.
    for _ in range(5):
        app.game.tap(A, base_reward=1)
    await feed(cb_update(A, "adv:missions:story"))
    claim = next(v for v in callbacks(last_screen(session)) if v.startswith("adv:mission:first_taps:"))
    await feed(cb_update(A, claim))
    assert session.of(AnswerCallbackQuery)[-1].text == "🎁 +20 XP"
    await feed(cb_update(A, "adv:skills"))
    skill = next(v for v in callbacks(last_screen(session)) if v.startswith("adv:skill:navigation:"))
    await feed(cb_update(A, skill))
    await feed(cb_update(A, "adv:routes"))
    route = next(v for v in callbacks(last_screen(session)) if v.startswith("adv:go:trail:"))
    await feed(cb_update(A, route))
    trip = app.game.overview(A)["expedition"]
    assert trip and "Возвращение:" in last_screen(session).text
    await feed(cb_update(A, f"adv:claim:{trip['id']}"))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    clock[0] = trip["ready_at"]
    await feed(cb_update(A, f"adv:claim:{trip['id']}"))
    assert "Лист следопыта" in session.of(AnswerCallbackQuery)[-1].text
    await feed(cb_update(A, "adv:collection"))
    assert "Первый маршрут" in last_screen(session).text
    assert app.db.user(A)["balance"] == 5
    assert app.game.overview(A)["profile"]["xp_total"] == 35
    await feed(msg_update(A, "👤 Профиль"))
    assert "Общий опыт: 35 XP" in last_screen(session).text
    app.db.conn.close()


@pytest.mark.parametrize("screen", ["adv:home", "adv:missions:story", "adv:missions:day", "adv:missions:week",
                                    "adv:routes", "adv:skills", "adv:collection", "adv:rank:month",
                                    "adv:rank:all", "adv:rank:balance"])
async def test_screens_are_bounded_and_have_a_way_back(tmp_path, bot, session, screen):
    app, feed, clock = await setup(tmp_path, bot)
    app.set("adventure_title", "<b>&" + "🌟" * 1000)
    app.set("adventure_intro", "<script>" + "🌟" * 3500)
    await feed(cb_update(A, screen))
    message = last_screen(session)
    text = html.unescape(re.sub(r"</?(?:b|i)>", "", message.text))
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert "<script>" not in message.text and "<b>&🌟" not in message.text
    if screen == "adv:home":
        assert "&lt;b&gt;&amp;" in message.text and "&lt;script&gt;" in message.text
    for data in callbacks(message):
        assert len(data.encode()) <= 64
    assert "adv:home" in callbacks(message)
    app.db.conn.close()


@pytest.mark.parametrize("data", ["adv:", "adv:unknown", "adv:rank:unknown", "adv:missions:unknown",
                                  "adv:go:trail:nan", "adv:go:unknown:0", "adv:skill:unknown:0",
                                  "adv:skill:navigation:-1", "adv:mission:missing:once", "adv:claim:١",
                                  "adv:claim:" + "9" * 50])
async def test_forged_callbacks_have_safe_error_and_no_reward(tmp_path, bot, session, data):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(cb_update(A, data))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert app.db.user(A)["balance"] == 0
    assert app.game.overview(A)["profile"]["xp_total"] == 0
    assert app.db.scalar("SELECT COUNT(*) FROM clicker_expeditions") == 0
    app.db.conn.close()


@pytest.mark.parametrize("kind", ["group", "other_private", "missing", "inaccessible"])
async def test_gameplay_callbacks_require_own_accessible_private_chat(tmp_path, bot, session, kind):
    app, feed, clock = await setup(tmp_path, bot)
    raw = cb_update(A, "adv:home").model_dump(exclude_none=True)
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
    assert app.db.scalar("SELECT COUNT(*) FROM clicker_profiles") == 0
    app.db.conn.close()


async def test_required_subscription_still_gates_adventure(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    app.set("required_channel", "@channel")
    session.member_status = "left"
    await feed(msg_update(A, "🧭 Приключение"))
    assert "подпишитесь" in session.texts()[-1]
    assert app.db.scalar("SELECT COUNT(*) FROM clicker_profiles") == 0
    app.db.conn.close()


async def test_opening_adventure_cancels_withdrawal_input(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    app.db.add_balance(A, 20000)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "🧭 Приключение"))
    assert "Экспедиция" in last_screen(session).text
    await feed(msg_update(A, "10000"))
    assert not any(m.text == app.s["withdraw_details_prompt"] for m in session.of(SendMessage))
    assert app.db.scalar("SELECT COUNT(*) FROM withdrawals") == 0
    assert app.db.user(A)["balance"] == 20000
    app.db.conn.close()


async def test_owner_can_see_gameplay_counts_without_claiming_user_rewards(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(msg_update(OWNER, "/start"))
    app.game.overview(A)
    await feed(cb_update(OWNER, "adm:stats"))
    assert "Профилей: 1" in last_screen(session).text
    assert app.db.scalar("SELECT COUNT(*) FROM clicker_xp_entries") == 0
    assert app.db.scalar("SELECT COUNT(*) FROM clicker_profiles") == 1
    app.db.conn.close()


async def test_other_templates_do_not_initialize_adventure(tmp_path, bot):
    for template in ("refbot", "starbot"):
        path = tmp_path / template
        path.mkdir()
        app, feed = make(path, template, bot)
        await feed(msg_update(A, "/start"))
        assert app.game is None
        assert not app.db.one("SELECT name FROM sqlite_master WHERE name='clicker_profiles'")
        app.db.conn.close()


async def test_refresh_not_modified_does_not_spam_new_messages(tmp_path, bot, session, monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    app, feed, clock = await setup(tmp_path, bot)
    original = session.make_request

    async def unchanged(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", unchanged)
    session.clear()
    await feed(cb_update(A, "adv:home"))
    assert not session.of(SendMessage)
    assert not session.of(AnswerCallbackQuery)[-1].show_alert
    app.db.conn.close()
