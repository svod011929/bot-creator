"""E2 display-only: paginated/filtered resale showcase, honest listing facts, partner accruals."""
from aiogram.methods import EditMessageText, SendMessage

from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import BUYER, OWNER, env  # noqa: F401

SELLER = 300


def listed(ctx, n, template="clicker", owner=SELLER, price=10000):
    ids = []
    for i in range(n):
        cur = ctx.db.conn.execute(
            "INSERT INTO bots(owner_id,template,token,username,sale_price,hosting_balance,vip,created_at) "
            "VALUES(?,?,?,?,?,?,?,0)", (owner, template, f"tok{template}{i}", f"{template}_{i}_bot", price + i, 450, 0))
        ids.append(cur.lastrowid)
    return ids


def kb(m):
    return [(b.text, b.callback_data) for row in m.reply_markup.inline_keyboard for b in row]


def snapshot(ctx):
    return [tuple(r) for r in ctx.db.all("SELECT * FROM bots ORDER BY id")], \
        [tuple(r) for r in ctx.db.all("SELECT id,balance FROM users ORDER BY id")], \
        ctx.db.scalar("SELECT COUNT(*) FROM transactions")


async def test_showcase_pages_and_filters(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(BUYER, "/start"))
    clickers = listed(ctx, 12)
    stars = listed(ctx, 2, template="starbot")
    listed(ctx, 1, owner=BUYER)  # own listing is never shown to its owner
    before = snapshot(ctx)
    await feed(msg_update(BUYER, "🛒 Перепродажа"))
    m = session.of(SendMessage)[-1]
    assert "Стр. 1/2 · всего 14" in m.text and "Комиссия перепродажи 5%" in m.text
    views = [d for _, d in kb(m) if d.startswith("rs:view:")]
    assert len(views) == 10 and views[0] == f"rs:view:{stars[-1]}"
    assert ("➡️", "rs:p:all:1") in kb(m) and ("• Все", "rs:p:all:0") in kb(m)
    await feed(cb_update(BUYER, "rs:p:all:1"))
    e = session.of(EditMessageText)[-1]
    assert "Стр. 2/2" in e.text and len([d for _, d in kb(e) if d.startswith("rs:view:")]) == 4
    await feed(cb_update(BUYER, "rs:p:starbot:0"))
    e = session.of(EditMessageText)[-1]
    assert "всего 2" in e.text and {d for _, d in kb(e) if d.startswith("rs:view:")} == {f"rs:view:{i}" for i in stars}
    await feed(cb_update(BUYER, "rs:p:refbot:0"))
    assert "В этом разделе ботов на продаже нет" in session.of(EditMessageText)[-1].text
    await feed(cb_update(BUYER, "rs:p:all:99"))  # clamps
    assert "Стр. 2/2" in session.of(EditMessageText)[-1].text
    for bad in ("rs:p:nope:0", "rs:p:all:x", "rs:view:x"):
        await feed(cb_update(BUYER, bad))
    assert str(SELLER) not in "".join(t.text for t in session.of(EditMessageText) + session.of(SendMessage))
    assert snapshot(ctx) == before and clickers


async def test_listing_shows_fee_and_balance_without_buying(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(BUYER, "/start"))
    [bot_id] = listed(ctx, 1, price=20050)
    ctx.db.change_balance(BUYER, 5000, "topup")
    before = snapshot(ctx)
    await feed(cb_update(BUYER, f"rs:view:{bot_id}"))
    t = session.of(EditMessageText)[-1]
    assert "Цена: <b>200,50 ₽</b>" in t.text and "Вы платите ровно цену" in t.text
    assert "Ваш баланс: 50 ₽ — не хватает 150,50 ₽" in t.text
    assert "хостинга и VIP остаются у бота" in t.text and "записей в базе бота" in t.text
    assert ("✅ Купить за 200,50 ₽", f"rs:buy:{bot_id}") in kb(t)
    assert str(SELLER) not in t.text
    assert snapshot(ctx) == before


async def test_own_listing_has_no_buy_button(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(SELLER, "/start"))
    [bot_id] = listed(ctx, 1)
    await feed(cb_update(SELLER, f"rs:view:{bot_id}"))
    t = session.of(EditMessageText)[-1]
    assert "Это ваш бот" in t.text and not [d for _, d in kb(t) if d.startswith("rs:buy:")]


async def test_seller_screen_shows_net_income(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(SELLER, "/start"))
    listed(ctx, 1, price=20050)
    await feed(cb_update(SELLER, "rs:mine"))
    t = session.of(EditMessageText)[-1].text
    # same formula as the purchase: 200,50 - floor(5%) = 190,48
    assert "Комиссия перепродажи 5%" in t and "получите 190,48 ₽" in t


async def test_partner_accruals_summary(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(BUYER, f"/start ref{OWNER}"))
    ctx.db.credit_referrer(BUYER, 10000, 10)
    ctx.db.credit_referrer(BUYER, 5000, 10)
    ctx.db.conn.execute("UPDATE transactions SET created_at=created_at-86400*40 WHERE id=(SELECT MIN(id) FROM "
                        "transactions WHERE kind='ref')")
    before = snapshot(ctx)
    await feed(msg_update(OWNER, "👥 Партнёры"))
    m = session.of(SendMessage)[-1]
    assert "Начислений: 2 на 15 ₽" in m.text and "за 30 дней: 5 ₽" in m.text and "Последнее:" in m.text
    assert "уже начисленное не пересчитывается" in m.text and str(BUYER) not in m.text
    assert ("📜 Партнёрские начисления", "cab:h:ref:0") in kb(m)
    await feed(cb_update(OWNER, "cab:h:ref:0"))
    assert "Партнёрские" in session.of(EditMessageText)[-1].text
    assert snapshot(ctx) == before


async def test_partner_without_accruals(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(OWNER, "👥 Партнёры"))
    assert "Начислений пока нет" in session.of(SendMessage)[-1].text
