"""E1: paginated, filtered, private, read-only operations history."""
from aiogram.methods import EditMessageText

from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import OWNER, REFERRER, env  # noqa: F401


def kb(m):
    return [(b.text, b.callback_data) for row in m.reply_markup.inline_keyboard for b in row]


def snapshot(ctx):
    return (ctx.db.all("SELECT * FROM transactions ORDER BY id"), ctx.db.all("SELECT id,balance FROM users ORDER BY id"))


async def seed(ctx, feed):
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(REFERRER, "/start"))
    ctx.db.change_balance(OWNER, 100000, "topup", "Пополнение через LavePay")
    for i in range(12):
        ctx.db.change_balance(OWNER, -100, "hosting", f"Хостинг бота ID {i}")
    ctx.db.change_balance(OWNER, 500, "ref", "10% от оплаты реферала 123456789")
    ctx.db.change_balance(OWNER, -50, "admin", "Администратор 987654321", allow_negative=True)
    ctx.db.change_balance(OWNER, 70, "weird_kind", "<b>x</b> 5555555555")
    ctx.db.change_balance(REFERRER, 999, "topup", "secret-other-user")


async def test_pages_and_filters(env):  # noqa: F811
    ctx, feed, session = env
    await seed(ctx, feed)
    before = snapshot(ctx)
    await feed(msg_update(OWNER, "💼 Мой кабинет"))
    await feed(cb_update(OWNER, "cab:history"))
    page = session.of(EditMessageText)[-1]
    assert "Стр. 1/2" in page.text and "всего 16" in page.text and "secret-other-user" not in page.text
    assert ("➡️", "cab:h:all:1") in kb(page) and ("⬅️ В кабинет", "cab:main") in kb(page)
    assert sum(1 for t, d in kb(page) if d.startswith("cab:tx:")) == 10
    await feed(cb_update(OWNER, "cab:h:all:1"))
    assert "Стр. 2/2" in session.of(EditMessageText)[-1].text
    await feed(cb_update(OWNER, "cab:h:bots:0"))
    t = session.of(EditMessageText)[-1].text
    assert "всего 12" in t and "Хостинг" in t and "Пополнение" not in t
    await feed(cb_update(OWNER, "cab:h:topup:0"))
    assert "всего 1" in session.of(EditMessageText)[-1].text
    await feed(cb_update(OWNER, "cab:h:other:0"))
    t = session.of(EditMessageText)[-1].text
    assert "всего 2" in t
    await feed(cb_update(OWNER, "cab:h:ref:99"))  # clamps
    assert "Стр. 1/1" in session.of(EditMessageText)[-1].text
    assert snapshot(ctx) == before


async def test_detail_is_private_and_redacted(env):  # noqa: F811
    ctx, feed, session = env
    await seed(ctx, feed)
    ids = {r["kind"]: r["id"] for r in ctx.db.all("SELECT id,kind FROM transactions WHERE user_id=?", OWNER)}
    other = ctx.db.scalar("SELECT id FROM transactions WHERE user_id=?", REFERRER)
    await feed(cb_update(OWNER, f"cab:tx:{ids['ref']}:all:0"))
    d = session.of(EditMessageText)[-1]
    assert "+5 ₽" in d.text and "Партнёрские" in d.text and "123456789" not in d.text and "UTC" in d.text
    assert ("⛔ Оповестить о проблеме", "info:report") in kb(d) and ("⬅️ К списку", "cab:h:all:0") in kb(d)
    await feed(cb_update(OWNER, f"cab:tx:{ids['admin']}:all:0"))
    assert "987654321" not in session.of(EditMessageText)[-1].text
    await feed(cb_update(OWNER, f"cab:tx:{ids['weird_kind']}:other:0"))
    t = session.of(EditMessageText)[-1].text
    assert "&lt;b&gt;x&lt;/b&gt;" in t and "5555555555" not in t and "weird_kind" in t
    n = len(session.of(EditMessageText))
    await feed(cb_update(OWNER, f"cab:tx:{other}:all:0"))
    assert len(session.of(EditMessageText)) == n  # someone else's operation is not shown


async def test_bad_callbacks_and_empty(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "cab:history"))
    assert "Операций пока нет" in session.of(EditMessageText)[-1].text
    n = len(session.of(EditMessageText))
    bad = ("cab:h:nope:0", "cab:h:all:-1", "cab:h:all:x", "cab:tx:abc:all:0", "cab:tx:1:all",
           "cab:h:all:99999999999999999999")
    for data in bad:
        await feed(cb_update(OWNER, data))
    assert len(session.of(EditMessageText)) == n
