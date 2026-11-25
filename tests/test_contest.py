"""Конкурсы конструктора: таблица, создание и завершение без финансовых правок."""
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from tests.conftest import cb_update, msg_update
from tests.test_admin_panel import _child_users, seed_bots
from tests.test_creator_flow import ADMIN, BUYER, OWNER, env  # noqa: F401


def _inline(message):
    return [(b.text, b.callback_data) for r in message.reply_markup.inline_keyboard for b in r]


async def test_info_and_admin_tools_show_contest_and_keep_integration_keys(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(OWNER, "📰 Информация"))
    labels = _inline(session.of(SendMessage)[-1])
    assert ("🏆 Конкурс", "contest:view") in labels
    assert ("❓ Помощь", "info:help") in labels
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "a:g:tools"))
    tools = _inline(session.of(EditMessageText)[-1])
    assert ("🏆 Конкурсы", "contest:view") in tools
    assert ("🔄 Перезапустить все боты", "a:restartall") in tools
    await feed(cb_update(ADMIN, "a:g:settings"))
    settings = _inline(session.of(EditMessageText)[-1])
    assert ("🔑 Ключи интеграций", "ik:home") in settings


async def test_no_active_contest_and_owner_cannot_manage(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "contest:view"))
    text = session.of(EditMessageText)[-1].text
    assert "нет активного конкурса" in text.lower()
    buttons = _inline(session.of(EditMessageText)[-1])
    assert ("🔄 Обновить", "contest:view") in buttons
    assert ("⬅️ Назад", "info:back") in buttons
    assert all("a:contest:" not in (d or "") for _, d in buttons)
    await feed(cb_update(OWNER, "a:contest:new"))
    assert session.of(AnswerCallbackQuery)[-1].text == "Нет доступа"
    await feed(cb_update(OWNER, "a:contest:end"))
    assert session.of(AnswerCallbackQuery)[-1].text == "Нет доступа"
    assert ctx.db.contest_active() is None


async def test_admin_creates_and_ends_contest(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "contest:view"))
    assert "нет активного конкурса" in session.of(EditMessageText)[-1].text.lower()
    buttons = _inline(session.of(EditMessageText)[-1])
    assert ("➕ Создать конкурс", "a:contest:new") in buttons
    assert ("🏁 Завершить текущий", "a:contest:end") in buttons
    assert ("⬅️ Назад", "a:main") in buttons

    await feed(cb_update(ADMIN, "a:contest:end"))
    assert session.of(AnswerCallbackQuery)[-1].text == "Нет активного конкурса"

    await feed(cb_update(ADMIN, "a:contest:new"))
    assert "Название конкурса" in session.of(SendMessage)[-1].text
    await feed(msg_update(ADMIN, "  "))
    assert "Введите название" in session.texts()[-1]
    await feed(msg_update(ADMIN, "Гонка <ботов>"))
    assert "призового фонда" in session.texts()[-1]
    await feed(msg_update(ADMIN, "3000 ₽ / 1500 / 500"))
    assert "Длительность" in session.texts()[-1]
    await feed(msg_update(ADMIN, "0"))
    assert "от 1 до 365" in session.texts()[-1]
    await feed(msg_update(ADMIN, "7"))
    text = session.texts()[-1]
    assert "Конкурс создан" in text
    assert "Гонка &lt;ботов&gt;" in text
    assert "3000 ₽ / 1500 / 500" in text
    assert "Осталось: <b>6 дн." in text or "Осталось: <b>7 дн." in text
    active = ctx.db.contest_active()
    assert active is not None and active["title"] == "Гонка <ботов>"
    first_id = active["id"]

    await feed(cb_update(ADMIN, "a:contest:new"))
    await feed(msg_update(ADMIN, "Второй"))
    await feed(msg_update(ADMIN, "Приз"))
    await feed(msg_update(ADMIN, "3"))
    second = ctx.db.contest_active()
    assert second is not None and second["id"] != first_id and second["title"] == "Второй"
    ended = ctx.db.one("SELECT status FROM contests WHERE id=?", first_id)
    assert ended["status"] == "ended"

    await feed(cb_update(ADMIN, "a:contest:end"))
    assert "Конкурс завершён" in session.of(EditMessageText)[-1].text
    assert ctx.db.contest_active() is None


async def test_leaderboard_scores_users_plus_op_earnings(env):  # noqa: F811
    ctx, feed, session = env
    owner_bots = seed_bots(ctx, 1, owner=OWNER)
    owner_bot = owner_bots[0]
    all_bots = seed_bots(ctx, 1, owner=BUYER)
    buyer_bot = next(i for i in all_bots if i not in owner_bots)
    _child_users(ctx, owner_bot, [(10, {}), (11, {})])
    _child_users(ctx, buyer_bot, [(20, {}), (21, {}), (22, {}), (23, {}), (24, {})])
    ctx.db.upsert_user(OWNER, "alice", "Alice")
    ctx.db.upsert_user(BUYER, "bob", "Bob")
    assert ctx.db.tgrass_credit(owner_bot, 10, 1, 15000)
    assert ctx.db.pf_credit(buyer_bot, 20, "https://t.me/x", 200)

    cid = ctx.db.contest_create("Зачёт", "приз", 10)
    assert cid > 0
    from creator.contest import leaderboard_text, score_owners

    scores = {s["owner_id"]: s for s in score_owners(ctx)}
    assert scores[OWNER]["users"] == 2 and scores[OWNER]["earnings_rub"] == 150
    assert scores[OWNER]["points"] == 152
    assert scores[BUYER]["users"] == 5 and scores[BUYER]["earnings_rub"] == 2
    assert scores[BUYER]["points"] == 7
    order = [s["owner_id"] for s in score_owners(ctx)]
    assert order[0] == OWNER and order[1] == BUYER

    text = leaderboard_text(ctx, BUYER)
    assert "🥇 @alice — <b>152</b>" in text
    assert "🥈 @bob — <b>7</b>" in text
    assert "Ваше место: <b>2</b>" in text
    assert "очки: <b>7</b>" in text


async def test_expired_contest_is_not_active_and_create_validates_days(env):  # noqa: F811
    ctx, feed, session = env
    cid = ctx.db.contest_create("Старый", "приз", 1)
    ctx.db.conn.execute("UPDATE contests SET ends_at=? WHERE id=?", (1, cid))
    assert ctx.db.contest_active() is None
    try:
        ctx.db.contest_create("x", "y", 0)
        raise AssertionError("days=0 must be rejected")
    except ValueError:
        pass
    try:
        ctx.db.contest_create("x", "y", 366)
        raise AssertionError("days=366 must be rejected")
    except ValueError:
        pass
    # opening a fresh DB still has the additive table
    row = ctx.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='contests'")
    assert row is not None
