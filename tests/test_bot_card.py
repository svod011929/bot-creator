"""Карточка бота: юзеры/хостинг/сети, клавиатура, подтверждения владения."""
from __future__ import annotations

import sqlite3

from aiogram.methods import EditMessageText, SendMessage

from creator.keyboards import bot_card_kb
from creator.tgrass import child_users_count, child_users_for_card
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, BUYER, OWNER, create_paid_bot, env  # noqa: F401


def last_kb(session):
    return [r for r in session.requests if isinstance(r, (SendMessage, EditMessageText)) and r.reply_markup][-1]


def kb_texts(m):
    return [b.text for row in m.reply_markup.inline_keyboard for b in row]


def kb_rows(m):
    return [[b.text for b in row] for row in m.reply_markup.inline_keyboard]


# ---------- helper ----------
def test_child_users_for_card_missing_is_zero(tmp_path):
    assert child_users_for_card(tmp_path / "nope") == 0
    assert child_users_count(tmp_path / "nope") == 0


def test_child_users_for_card_counts(tmp_path):
    db = tmp_path / "bot.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO users(id) VALUES (?)", [(1,), (2,), (3,)])
    conn.commit()
    conn.close()
    assert child_users_for_card(tmp_path) == 3
    assert child_users_count(tmp_path) == 3


def test_child_users_for_card_corrupt_is_none_count_zero(tmp_path):
    (tmp_path / "bot.db").write_bytes(b"not a sqlite database!!!")
    assert child_users_for_card(tmp_path) is None
    assert child_users_count(tmp_path) == 0  # gate semantics unchanged


# ---------- card text / kb ----------
async def test_card_shows_users_hosting_networks_and_grouped_kb(env):  # noqa: F811
    from creator.piarflow import PiarFlowClient, PiarFlowService
    from creator.tgrass import TGrassService
    from tests.test_tgrass import FakeClient

    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)

    # child db with 2 users
    bdir = ctx.sup.bot_dir(bot_id)
    bdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(bdir / "bot.db")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO users(id) VALUES (?)", [(10,), (11,)])
    conn.commit()
    conn.close()

    # tiny hosting balance → «меньше суток»
    ctx.db.update_bot(bot_id, hosting_balance=100)  # 1₽ < day price 4.50₽
    ctx.tgrass = TGrassService(ctx, FakeClient())
    ctx.cfg.tgrass_factory_token = "factory"
    ctx.piarflow = PiarFlowService(ctx, PiarFlowClient("acct", "http://invalid.invalid"))
    ctx.cfg.piarflow_token = "acct"
    ctx.db.tgrass_apply(bot_id, OWNER, 2)
    ctx.db.pf_apply(bot_id, OWNER, 2)

    session.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    card = last_kb(session)
    assert "Пользователи: 2" in card.text
    assert "меньше суток" in card.text
    assert "TGrass:" in card.text and "заявк" in card.text.lower()
    assert "PiarFlow:" in card.text
    assert "factory" not in card.text.lower() and "relay" not in card.text.lower()
    assert "Режим:" not in card.text

    rows = kb_rows(card)
    flat = kb_texts(card)
    assert "🔄 Перезапустить" in flat or "▶️ Запустить" in flat or "⏹ Остановить" in flat
    assert "🔄 Обновить" not in flat
    # transfer and delete in last rows (after main), not beside restart
    assert any("🗑 Удалить" in r for r in rows)
    assert any("🔁 Передать бота" in r or "🔁 Передать" in r for r in rows)
    # restart/start row must not contain transfer
    for r in rows:
        if "🔄 Перезапустить" in r or "▶️ Запустить" in r or "⏹ Остановить" in r:
            assert "🔁 Передать бота" not in r and "🔁 Передать" not in r
            assert "🗑 Удалить" not in r


async def test_card_users_nd_on_corrupt_db(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    bdir = ctx.sup.bot_dir(bot_id)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "bot.db").write_bytes(b"corrupt")
    session.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    assert "Пользователи: н/д" in last_kb(session).text


async def test_card_hosting_unlimited_when_day_price_zero(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    ctx.db.kv_set("price:HOSTING_DAY_PRICE", "0")
    session.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    assert "безлимит" in last_kb(session).text


async def test_stale_transfer_ok_and_delok_after_ownership_change(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(BUYER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    # OWNER confirms transfer to BUYER
    await feed(msg_update(OWNER, str(BUYER)))  # wrong state — need transfer state
    await feed(cb_update(OWNER, f"b:{bot_id}:transfer"))
    await feed(msg_update(OWNER, str(BUYER)))
    await feed(cb_update(OWNER, f"b:{bot_id}:transfer_ok:{BUYER}"))
    assert ctx.db.bot(bot_id)["owner_id"] == BUYER

    # stale callbacks from old owner
    session.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:delok"))
    assert ctx.db.bot(bot_id)["deleted"] == 0
    await feed(cb_update(OWNER, f"b:{bot_id}:transfer_ok:{ADMIN}"))
    assert ctx.db.bot(bot_id)["owner_id"] == BUYER

    # new owner can delete with confirm
    await feed(cb_update(BUYER, f"b:{bot_id}:del"))
    await feed(cb_update(BUYER, f"b:{bot_id}:delok"))
    # bot() фильтрует deleted=0 — после удаления записи нет
    assert ctx.db.bot(bot_id) is None
    assert ctx.db.scalar("SELECT deleted FROM bots WHERE id=?", bot_id) == 1


def test_bot_card_kb_layout_unit():
    b = {"id": 1, "template": "clicker", "desired": 1, "status": "running", "sale_price": None}
    kb = bot_card_kb(b, for_sale=False, tgrass=True)
    rows = [[x.text for x in row] for row in kb.inline_keyboard]
    flat = [t for r in rows for t in r]
    assert "🔄 Перезапустить" in flat
    assert "🔄 Обновить" not in flat
    assert rows[-2] == ["🔁 Передать бота"] or "🔁 Передать бота" in rows[-2] or rows[-3][0].startswith("🔁")
    # last non-back dangerous + back
    assert rows[-1] == ["⬅️ Назад"]
    assert any(r == ["🗑 Удалить"] or "🗑 Удалить" in r for r in rows)
