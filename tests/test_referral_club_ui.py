"""End-to-end club interactions via the real dispatcher and a fake Telegram transport."""

import html
import re

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Update

from child.files import write_config
from child.referral_club import SEED
from child.subscription import GateResult
from tests.conftest import cb_update, msg_update
from tests.test_child_bots import OWNER, A, B, make
from tests.test_clicker_adventure_ui import callbacks, last_screen


async def setup(tmp_path, bot):
    app, feed = make(tmp_path, "refbot", bot)
    clock = [1794301200]
    app.club.clock = lambda: clock[0]
    await feed(msg_update(A, "/start"))
    return app, feed, clock


async def click(feed, session, uid, prefix):
    data = next(c for c in callbacks(last_screen(session)) if c.startswith(prefix))
    await feed(cb_update(uid, data))
    return data


async def onboard(feed, session, uid):
    await feed(cb_update(uid, "club:onboard"))
    await click(feed, session, uid, "club:interest:learn:")
    await click(feed, session, uid, "club:onanswer:1:")


async def test_complete_club_session_preserves_legacy_cash_reward(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    assert "🚀 Клуб" in [b.text for row in session.of(SendMessage)[-1].reply_markup.keyboard for b in row]
    await feed(msg_update(A, "🚀 Клуб"))
    await onboard(feed, session, A)
    assert app.club.summary(A)["member"]["points"] == 20
    await feed(cb_update(A, "club:link:1"))
    token = re.search(r"start=(c_[A-Za-z0-9_-]+)", last_screen(session).text).group(1)
    await feed(msg_update(B, "/start " + token))
    assert app.db.user(A)["balance"] == 5
    assert app.club.summary(A)["qualified"] == 0
    await onboard(feed, session, B)
    await feed(cb_update(B, "club:quiz:1"))
    answer = await click(feed, session, B, "club:answer:1:1:")
    assert app.club.summary(A)["qualified"] == 1
    assert app.club.summary(A)["member"]["points"] == 45
    assert app.club.summary(B)["member"]["points"] == 30
    await feed(cb_update(B, answer))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert app.club.summary(A)["member"]["points"] == 45
    await feed(cb_update(A, "club:invites:0"))
    assert "✅ Засчитан" in last_screen(session).text
    await feed(msg_update(A, "👤 Профиль"))
    assert "Клубные баллы: 45" in last_screen(session).text
    assert app.db.user(A)["balance"] == 5
    app.db.conn.close()


async def test_owner_wizard_draft_edit_and_publish_flow(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(OWNER, "/admin"))
    assert "club:admin:0" in callbacks(last_screen(session))
    await feed(cb_update(OWNER, "club:new"))
    for value in ("Synthetic campaign", "Read the rules", "2", "45", "10", "7", "Which answer?", "First\nSecond\nThird"):
        await feed(msg_update(OWNER, value))
    await click(feed, session, OWNER, "club:correct:1:")
    assert "Предпросмотр" in last_screen(session).text
    await click(feed, session, OWNER, "club:field:description:")
    await feed(msg_update(OWNER, "The correct answer is Second."))
    await click(feed, session, OWNER, "club:save:")
    cid = app.db.scalar("SELECT MAX(id) FROM ref_campaigns")
    c = app.club.owner_campaign(OWNER, cid)
    assert c["status"] == "draft" and c["description"] == "The correct answer is Second."
    await click(feed, session, OWNER, "club:edit:")
    await click(feed, session, OWNER, "club:field:reward:")
    await feed(msg_update(OWNER, "60"))
    await click(feed, session, OWNER, "club:save:")
    await click(feed, session, OWNER, "club:publish_view:")
    assert app.club.owner_campaign(OWNER, cid)["status"] == "draft"
    publish = await click(feed, session, OWNER, "club:publish:")
    assert app.club.owner_campaign(OWNER, cid)["ends_at"] == clock[0] + 7 * 86400
    assert app.club.owner_campaign(OWNER, cid)["reward"] == 60
    await feed(cb_update(OWNER, publish))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    await feed(cb_update(OWNER, f"club:manage:{cid}"))
    await click(feed, session, OWNER, "club:pause:")
    assert app.club.owner_campaign(OWNER, cid)["status"] == "paused"
    await click(feed, session, OWNER, "club:activate:")
    assert app.club.owner_campaign(OWNER, cid)["status"] == "active"
    await feed(cb_update(OWNER, "adm:stats"))
    assert "активных кампаний: 2" in last_screen(session).text
    app.db.conn.close()


@pytest.mark.parametrize("target", ["/start", "👤 Профиль", "💸 Вывод", "/admin", "🚀 Клуб", "👥 Пригласить друзей"])
async def test_owner_can_leave_and_resume_persistent_wizard(tmp_path, bot, session, target):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "club:new"))
    await feed(msg_update(OWNER, "Saved title"))
    await feed(msg_update(OWNER, target))
    assert not app.club.wizard_get(OWNER)["active"]
    await feed(msg_update(OWNER, "ordinary message"))
    assert app.club.wizard_get(OWNER)["step"] == "description"
    await feed(cb_update(OWNER, "club:resume"))
    assert app.club.wizard_get(OWNER)["data"]["title"] == "Saved title"
    assert "шаг 2/9" in last_screen(session).text
    await feed(msg_update(OWNER, "/cancel"))
    assert app.club.wizard_get(OWNER) is None
    app.db.conn.close()


async def test_owner_permission_transfer_is_checked_without_restart(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(msg_update(OWNER, "/start"))
    cid = app.club.create_draft(OWNER, {**SEED, "days": 7})
    write_config(app.dir, {**app.cfg, "owner_id": B})
    await feed(cb_update(OWNER, f"club:publish:{cid}:1"))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert app.db.scalar("SELECT status FROM ref_campaigns WHERE id=?", cid) == "draft"
    await feed(cb_update(B, f"club:publish:{cid}:1"))
    assert app.db.scalar("SELECT status FROM ref_campaigns WHERE id=?", cid) == "active"
    await feed(cb_update(A, "club:manage:1"))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    app.db.conn.close()


@pytest.mark.parametrize(
    "data",
    [
        "club:",
        "club:unknown",
        "club:list:١",
        "club:list:-1",
        "club:list:" + "9" * 50,
        "club:campaign:99999",
        "club:answer:1:999:1",
        "club:interest:unknown:0",
        "club:link:1",
        "club:publish:1:1",
        "club:field:title:0",
        "club:new",
        "club:save:0",
    ],
)
async def test_forged_callback_has_safe_error_and_no_reward(tmp_path, bot, session, data):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(cb_update(A, data))
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    assert app.db.user(A)["balance"] == 0
    assert app.club.summary(A)["member"]["points"] == 0
    assert app.db.scalar("SELECT COUNT(*) FROM ref_campaigns") == 1
    app.db.conn.close()


@pytest.mark.parametrize("kind", ["group", "other_private", "missing", "inaccessible"])
async def test_club_callbacks_require_own_accessible_private_chat(tmp_path, bot, session, kind):
    app, feed, clock = await setup(tmp_path, bot)
    raw = cb_update(A, "club:home").model_dump(exclude_none=True)
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
    assert app.club.summary(A)["member"]["points"] == 0
    app.db.conn.close()


async def test_unknown_subscription_does_not_qualify_campaign(tmp_path, bot, session, monkeypatch):
    app, feed, clock = await setup(tmp_path, bot)
    await onboard(feed, session, A)
    token = app.club.personal_link(A, 1)
    await feed(msg_update(B, "/start c_" + token))

    async def unconfirmed(bot, uid):
        return GateResult.TEMPORARY_ACCESS

    monkeypatch.setattr(app, "gate_result", unconfirmed)
    await onboard(feed, session, B)
    await feed(cb_update(B, "club:quiz:1"))
    await click(feed, session, B, "club:answer:1:1:")
    assert app.club.summary(A)["qualified"] == 0
    assert app.club.summary(B)["member"]["points"] == 30
    await feed(cb_update(B, "club:check"))
    assert "пока не подтверждён" in session.of(AnswerCallbackQuery)[-1].text
    assert app.club.summary(A)["member"]["points"] == 20
    app.db.conn.close()


async def test_opening_club_leaves_withdrawal_flow_without_spending_points(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    await onboard(feed, session, A)
    app.db.add_balance(A, 200)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "🚀 Клуб"))
    assert "Статус:" in last_screen(session).text
    await feed(msg_update(A, "150"))
    assert app.db.scalar("SELECT COUNT(*) FROM withdrawals") == 0
    assert app.db.user(A)["balance"] == 200
    assert app.club.summary(A)["member"]["points"] == 20
    app.db.conn.close()


async def test_group_start_does_not_accept_campaign_attribution(tmp_path, bot, session):
    app, feed, clock = await setup(tmp_path, bot)
    await onboard(feed, session, A)
    token = app.club.personal_link(A, 1)
    await feed(msg_update(B, "/start c_" + token, chat={"id": -123, "type": "group"}))
    assert app.db.user(B)["referrer_id"] is None
    assert app.club.summary(A)["pending"] == 0
    assert app.db.user(A)["balance"] == 0
    app.db.conn.close()


async def test_unchanged_screen_does_not_send_extra_message(tmp_path, bot, session, monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    app, feed, clock = await setup(tmp_path, bot)
    original = session.make_request

    async def unchanged(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", unchanged)
    session.clear()
    await feed(cb_update(A, "club:home"))
    assert not session.of(SendMessage)
    assert not session.of(AnswerCallbackQuery)[-1].show_alert
    app.db.conn.close()


@pytest.mark.parametrize(
    "screen",
    [
        "club:home",
        "club:list:0",
        "club:campaign:1",
        "club:quiz:1",
        "club:invites:0",
        "club:rank",
        "club:achievements",
        "club:admin:0",
        "club:manage:1",
        "club:edit:2:1",
    ],
)
async def test_telegram_screen_limits_and_html_escaping(tmp_path, bot, session, screen):
    app, feed, clock = await setup(tmp_path, bot)
    await feed(msg_update(OWNER, "/start"))
    await onboard(feed, session, OWNER)
    app.set("club_title", "<script>" + "🌟" * 1000)
    app.set("club_intro", "<script>" + "🌟" * 3500)
    app.club.create_draft(
        OWNER,
        {
            **SEED,
            "days": 7,
            "title": "🌟" * 80,
            "description": "🌟" * 900,
            "question": "🌟" * 180,
            "answers": ["🌟" * 80, "🌈" * 80, "🌍" * 80],
        },
    )
    await feed(cb_update(OWNER, screen))
    message = last_screen(session)
    visible = html.unescape(re.sub(r"</?(?:b|i)>", "", message.text))
    assert len(visible.encode("utf-16-le")) // 2 <= 4096
    assert "<script>" not in message.text
    assert all(len(data.encode()) <= 64 for data in callbacks(message))
    assert (
        screen == "club:home"
        or "club:home" in callbacks(message)
        or any(data.startswith("club:save:") for data in callbacks(message))
    )
    app.db.conn.close()
