"""D1: child-bot wording for the bot's own unit and manual payouts; money logic unchanged."""
import time

from aiogram.methods import SendMessage

from tests.conftest import cb_update, msg_update
from tests.test_child_bots import OWNER, A, make


def owner_texts(session):
    return [m.text for m in session.of(SendMessage) if m.chat_id == OWNER]


def to_user(session):
    return [m.text for m in session.of(SendMessage) if m.chat_id == A]


async def request(app, feed, amount="60"):
    app.set("min_withdraw", 50)
    app.db.add_balance(A, 100)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, amount))
    await feed(msg_update(A, "card 4444"))


async def test_request_messages_and_owner_buttons(tmp_path, bot, session):
    app, feed = make(tmp_path, "starbot", bot)
    await feed(msg_update(A, "/start"))
    app.db.add_balance(A, 100)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "500"))
    assert "⭐" in session.texts()[-1] and "Сумма должна быть" in session.texts()[-1]
    await feed(msg_update(A, "60"))
    await feed(msg_update(A, "card 4444"))
    accepted = to_user(session)[-1]
    assert "Заявка №1" in accepted and "вручную" in accepted and "Мои заявки" in accepted
    owner_msg = [m for m in session.of(SendMessage) if m.chat_id == OWNER][-1]
    buttons = [(b.text, b.callback_data) for row in owner_msg.reply_markup.inline_keyboard for b in row]
    assert buttons == [("✅ Отметить выплаченной", "wd:ok:1"), ("❌ Отклонить", "wd:no:1")]
    assert app.db.user(A)["balance"] == 40


async def test_decision_notifications_do_not_claim_transfer(tmp_path, bot, session):
    app, feed = make(tmp_path, "clicker", bot)
    await feed(msg_update(A, "/start"))
    await request(app, feed)
    await feed(cb_update(OWNER, "wd:ok:1"))
    paid = to_user(session)[-1]
    assert "отметил заявку на вывод №1 как выплаченную" in paid and "✅ выплачена" not in paid
    await request(app, feed)
    await feed(cb_update(OWNER, "wd:no:2"))
    assert "№2 отклонена владельцем, средства возвращены на баланс" in to_user(session)[-1]
    assert app.db.user(A)["balance"] == 140  # 100-60 +100-60 +60 refund


async def test_detail_button_label(tmp_path, bot, session):
    app, feed = make(tmp_path, "clicker", bot)
    await feed(msg_update(A, "/start"))
    await request(app, feed)
    await feed(cb_update(OWNER, "wh:detail:pending:0:1"))
    last = [m for m in session.requests if getattr(getattr(m, "reply_markup", None), "inline_keyboard", None)][-1]
    labels = {b.text: b.callback_data for row in last.reply_markup.inline_keyboard for b in row}
    assert labels.get("✅ Отметить выплаченной", "").startswith("wd:ok:1") and "✅ Выплачено" not in labels
    assert "«Отмечена выплаченной» — ручная отметка владельца" in last.text


async def test_owner_stats_never_mix_units(tmp_path, bot, session):
    app, feed = make(tmp_path, "starbot", bot)
    await feed(msg_update(A, "/start"))
    now = int(time.time())
    for amount, unit, status in ((10, "⭐", "paid"), (5, "⭐", "paid"), (7, "<b>₽</b>", "paid"),
                                 (3, None, "paid"), (99, "⭐", "pending"), (50, "⭐", "rejected")):
        app.db.exec("INSERT INTO withdrawals(user_id,amount,details,status,created_at,currency_at_request) "
                    "VALUES(?,?,?,?,?,?)", A, amount, "x", status, now, unit)
    await feed(cb_update(OWNER, "adm:stats"))
    t = session.texts()[-1]
    assert "Отмечено выплаченными: 4" in t and "15 ⭐" in t and "7 &lt;b&gt;₽&lt;/b&gt;" in t
    assert "Историческая единица баланса не сохранена" in t
    assert "Недоступны для рассылки" in t and "по времени сервера" in t and "Заблокировали" not in t
    assert "Выплачено: 25" not in t


async def test_owner_stats_none_paid(tmp_path, bot, session):
    app, feed = make(tmp_path, "refbot", bot)
    await feed(msg_update(A, "/start"))
    await feed(cb_update(OWNER, "adm:stats"))
    assert "Отмечено выплаченными: 0" in session.texts()[-1]
