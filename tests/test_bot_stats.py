"""C4 minimal: read-only owner statistics from data already stored in the bot's own database."""
import hashlib
import time

from aiogram.methods import AnswerCallbackQuery, EditMessageText

from child.db import ChildDB
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, BUYER, OWNER, create_paid_bot, env  # noqa: F401


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def seed(ctx, bot_id):
    now = int(time.time())
    db = ChildDB(ctx.sup.bot_dir(bot_id) / "bot.db")
    rows = [(1, None, 0, now - 60), (2, 1, 0, now - 3600 * 23), (3, 1, 1, now - 86400 * 3),
            (4, None, 0, now - 86400 * 20), (5, None, 1, now - 86400 * 40), (6, 2, 0, now - 86400 - 5)]
    for uid, ref, blocked, created in rows:
        db.exec("INSERT INTO users(id,username,full_name,referrer_id,blocked,created_at) VALUES(?,?,?,?,?,?)",
                uid, f"u{uid}", "N", ref, blocked, created)
    db.exec("INSERT INTO tasks(title,url,reward,active) VALUES('a','https://t.me/a',1,1)")
    db.exec("INSERT INTO tasks(title,url,reward,active) VALUES('b','https://t.me/b',1,0)")
    for uid, tid in ((1, 1), (2, 1), (2, 2)):
        db.exec("INSERT INTO task_done(user_id,task_id) VALUES(?,?)", uid, tid)
    for status in ("pending", "pending", "paid", "rejected"):
        db.exec("INSERT INTO withdrawals(user_id,amount,details,status,created_at) VALUES(1,10,'x',?,?)", status, now)
    db.conn.commit()
    db.conn.close()


def last_edit(session):
    return session.of(EditMessageText)[-1]


async def test_card_has_stats_button_and_screen_definitions(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    seed(ctx, bot_id)
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    card = [(b.text, b.callback_data) for r in last_edit(session).reply_markup.inline_keyboard for b in r]
    assert ("📊 Статистика", f"bst:{bot_id}") in card
    path = ctx.sup.bot_dir(bot_id) / "bot.db"
    before, creator_before = digest(path), digest(ctx.cfg.db_path)
    await feed(cb_update(OWNER, f"bst:{bot_id}"))
    m = last_edit(session)
    t = m.text
    assert "Пользователей всего: 6" in t
    assert "24 ч — 2" in t and "7 дней — 4" in t and "30 дней — 5" in t
    assert "по реферальной ссылке: 3" in t and "недоступными при рассылке: 2" in t
    assert "активных 1" in t and "выполнений 3" in t and "пользователей 2" in t
    assert "на рассмотрении 2" in t and "отмечено выплаченными 1" in t and "отклонено 1" in t
    assert "Не измеряется" in t and "u1" not in t and len(t) < 4096
    kb = [(b.text, b.callback_data) for r in m.reply_markup.inline_keyboard for b in r]
    from creator.engagement import request
    assert kb == [("📈 Активность и возвращения", request(bot_id)),
                  ("🔄 Обновить", f"bst:{bot_id}"), ("⬅️ К боту", f"b:{bot_id}:open")]
    assert digest(path) == before and digest(ctx.cfg.db_path) == creator_before


async def test_missing_and_broken_databases_are_not_zero(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    path = ctx.sup.bot_dir(bot_id) / "bot.db"
    if path.exists():
        path.unlink()
    await feed(cb_update(OWNER, f"bst:{bot_id}"))
    assert "Данных пока нет" in last_edit(session).text
    assert not path.exists()  # opening stats never creates the database
    path.write_bytes(b"not a database" * 100)
    await feed(cb_update(OWNER, f"bst:{bot_id}"))
    t = last_edit(session).text
    assert "Не удалось прочитать" in t and "Пользователей всего: 0" not in t
    path.unlink()
    import sqlite3
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, referrer_id INTEGER, blocked INTEGER, created_at INTEGER)")
    c.execute("INSERT INTO users VALUES (1, NULL, 0, 1)")
    c.commit()
    c.close()
    await feed(cb_update(OWNER, f"bst:{bot_id}"))
    t = last_edit(session).text
    assert "Пользователей всего: 1" in t and t.count("нет данных") >= 2


async def test_access_is_owner_private_and_current(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    seed(ctx, bot_id)
    await feed(msg_update(BUYER, "/start"))
    await feed(msg_update(ADMIN, "/start"))
    session.clear()
    for uid, data in ((BUYER, f"bst:{bot_id}"), (ADMIN, f"bst:{bot_id}"), (OWNER, "bst:x"), (OWNER, "bst:0"),
                      (OWNER, f"bst:{bot_id}:1"), (OWNER, "bst:99999")):
        await feed(cb_update(uid, data))
    assert not session.of(EditMessageText)
    assert all(a.text == "Бот не найден или действие устарело." for a in session.of(AnswerCallbackQuery))
    with ctx.db.tx() as c:
        c.execute("UPDATE bots SET owner_id=? WHERE id=?", (BUYER, bot_id))
    await feed(cb_update(OWNER, f"bst:{bot_id}"))
    assert not session.of(EditMessageText)
    await feed(cb_update(BUYER, f"bst:{bot_id}"))
    assert "Пользователей всего: 6" in last_edit(session).text


async def test_foreign_wizard_is_kept(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    await feed(cb_update(OWNER, "tpl:clicker"))
    session.clear()
    await feed(cb_update(OWNER, f"bst:{bot_id}"))
    assert not session.of(EditMessageText) and "/cancel" in session.of(AnswerCallbackQuery)[-1].text
    await feed(msg_update(OWNER, "not a token"))
    assert "токен" in session.texts()[-1]  # the token wizard is still waiting


async def test_username_is_escaped(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    with ctx.db.tx() as c:
        c.execute("UPDATE bots SET username=? WHERE id=?", ("a<b>&", bot_id))
    await feed(cb_update(OWNER, f"bst:{bot_id}"))
    assert "a&lt;b&gt;&amp;" in last_edit(session).text
